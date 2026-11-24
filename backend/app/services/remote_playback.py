"""Playback shared between one account's open clients (web tabs, iOS app), like a "connect" feature.

One room per Jellyfin user, kept in memory (the app runs one process). Each open client is a device on a
WebSocket. At most one device plays the audio (the active device); it publishes the playback state, and the
other devices mirror it and send commands that the server forwards to it. A device becomes active by claiming
(it starts playing while no device is active, or the user picks "play on this device"); the previous active
device is told to release (stop its audio and mirror). When the active device leaves, the state stays, paused,
with no active device, so the next device to play continues from there.

Messages are JSON objects with a `type`:
- client -> server: `hello` (first: `device_id`, `name`, `kind`, `playing`), `state` (active device only),
  `claim`, `command` (`action`, `args`).
- server -> client: `welcome` (`device_id`), `devices` (`devices`, `active`), `state` (`state`; `queue` only
  when it changed for that device), `command`, `release`, `granted` (`state`, `queue`, `position`).
"""
import asyncio
import re
import time
from typing import Optional

from fastapi import WebSocket

MAX_QUEUE = 3000
MAX_DEVICES = 12
JELLYFIN_ID = re.compile(r"^[0-9a-f]{32}$")
DEVICE_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
KINDS = {"web", "ios"}
ACTIONS = {
    "play", "pause", "toggle", "next", "previous", "seek", "skipTo", "playTracks", "playNext", "addToQueue",
    "removeAt", "repeat", "shuffle",
}
REPEAT_MODES = {"none", "all", "single"}
TRACK_TEXT = ("title", "artist", "album", "path")
TRACK_IDS = ("artist_id", "album_id", "playlist_entry_id")


def _text(value, limit: int = 500) -> str:
    return str(value)[:limit] if isinstance(value, (str, int, float)) else ""


def clean_track(raw) -> Optional[dict]:
    """A library track as both clients use it; URLs are rebuilt from the ID, never taken from a client."""
    if not isinstance(raw, dict):
        return None
    track_id = raw.get("id")
    if not isinstance(track_id, str) or not JELLYFIN_ID.match(track_id):
        return None
    track = {key: _text(raw.get(key)) for key in TRACK_TEXT}
    for key in TRACK_IDS:
        value = raw.get(key)
        track[key] = value if isinstance(value, str) and len(value) <= 64 else None
    for key in ("duration_sec", "index_number", "disc_number"):
        value = raw.get(key)
        track[key] = value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    track["duration_sec"] = track["duration_sec"] or 0
    track["has_lyrics"] = bool(raw.get("has_lyrics"))
    track["is_favorite"] = bool(raw.get("is_favorite"))
    track["id"] = track_id
    track["stream_url"] = f"/api/jellyfin/stream/{track_id}"
    track["cover_url"] = f"/api/jellyfin/image/{track_id}"
    playlist_id = raw.get("playlist_id")
    track["playlist_id"] = playlist_id if isinstance(playlist_id, str) and JELLYFIN_ID.match(playlist_id) else None
    return track


def clean_queue(raw) -> Optional[list]:
    if not isinstance(raw, list):
        return None
    return [track for track in (clean_track(item) for item in raw[:MAX_QUEUE]) if track]


def _number(value, default: float = 0.0) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def clean_args(action: str, raw) -> Optional[dict]:
    args = raw if isinstance(raw, dict) else {}
    if action == "seek":
        return {"position": max(0.0, _number(args.get("position")))}
    if action in ("skipTo", "removeAt"):
        index = args.get("index")
        return {"index": index} if isinstance(index, int) and not isinstance(index, bool) and index >= 0 else None
    if action == "playTracks":
        tracks = clean_queue(args.get("tracks"))
        if not tracks:
            return None
        index = args.get("index")
        index = index if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(tracks) else 0
        playlist_id = args.get("playlist_id")
        playlist_id = playlist_id if isinstance(playlist_id, str) and JELLYFIN_ID.match(playlist_id) else None
        return {"tracks": tracks, "index": index, "shuffle": bool(args.get("shuffle")), "playlist_id": playlist_id}
    if action in ("playNext", "addToQueue"):
        track = clean_track(args.get("track"))
        return {"track": track} if track else None
    if action == "repeat":
        mode = args.get("mode")
        return {"mode": mode} if mode in REPEAT_MODES else None
    if action == "shuffle":
        return {"on": bool(args.get("on"))}
    return {}


class Device:
    def __init__(self, socket: WebSocket, device_id: str, name: str, kind: str):
        self.socket = socket
        self.id = device_id
        self.name = name
        self.kind = kind
        # Queue revision this device last received, so the queue is sent only when it changed.
        self.queue_rev = -1

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "kind": self.kind}


class Room:
    def __init__(self):
        self.devices: dict[str, Device] = {}
        self.active: Optional[str] = None
        self.queue: list = []
        self.queue_rev = 0
        # index, position, playing, repeat, shuffle, updated (server time of `position`)
        self.state: Optional[dict] = None
        self.lock = asyncio.Lock()

    def position_now(self) -> float:
        if not self.state:
            return 0.0
        position = self.state["position"]
        if self.state["playing"]:
            position += time.time() - self.state["updated"]
        return position

    def state_message(self, device: Device) -> dict:
        state = dict(self.state) if self.state else None
        if state is not None:
            state["position"] = self.position_now()
            state["updated"] = time.time()
            state["active"] = self.active
        message = {"type": "state", "state": state, "queue_rev": self.queue_rev}
        if device.queue_rev != self.queue_rev:
            message["queue"] = self.queue
            device.queue_rev = self.queue_rev
        return message

    def devices_message(self) -> dict:
        return {"type": "devices", "devices": [d.public() for d in self.devices.values()], "active": self.active}


async def _send(device: Device, message: dict) -> None:
    try:
        await device.socket.send_json(message)
    except Exception:
        # A closed socket is removed by its own receive loop.
        pass


class RemotePlayback:
    rooms: dict[str, Room] = {}

    @classmethod
    def room(cls, user_id: str) -> Room:
        return cls.rooms.setdefault(user_id, Room())

    @classmethod
    async def broadcast_devices(cls, room: Room) -> None:
        message = room.devices_message()
        await asyncio.gather(*(_send(d, message) for d in list(room.devices.values())))

    @classmethod
    async def broadcast_state(cls, room: Room, skip: Optional[str] = None) -> None:
        await asyncio.gather(*(_send(d, room.state_message(d)) for d in list(room.devices.values()) if d.id != skip))

    @classmethod
    async def join(cls, user_id: str, socket: WebSocket, hello: dict) -> Optional[Device]:
        device_id = hello.get("device_id")
        if not isinstance(device_id, str) or not DEVICE_ID.match(device_id):
            return None
        kind = hello.get("kind") if hello.get("kind") in KINDS else "web"
        name = _text(hello.get("name"), 60).strip() or ("iPhone" if kind == "ios" else "Web")
        room = cls.room(user_id)
        async with room.lock:
            old = room.devices.get(device_id)
            if old and old.socket is not socket:
                # The same device reconnected (network change): the newer socket wins.
                try:
                    await old.socket.close()
                except Exception:
                    pass
            if not old and len(room.devices) >= MAX_DEVICES:
                return None
            device = Device(socket, device_id, name, kind)
            room.devices[device_id] = device
            release = False
            if hello.get("playing") is True:
                # Still playing after a reconnect: it keeps (or takes back) the audio unless another device has it.
                if room.active in (None, device_id):
                    room.active = device_id
                else:
                    release = True
        await _send(device, {"type": "welcome", "device_id": device_id})
        if release:
            await _send(device, {"type": "release"})
        await cls.broadcast_devices(room)
        await _send(device, room.state_message(device))
        return device

    @classmethod
    async def leave(cls, user_id: str, device: Device) -> None:
        room = cls.rooms.get(user_id)
        if not room or room.devices.get(device.id) is not device:
            return
        async with room.lock:
            room.devices.pop(device.id, None)
            was_active = room.active == device.id
            if was_active:
                room.active = None
                if room.state:
                    room.state["position"] = room.position_now()
                    room.state["playing"] = False
                    room.state["updated"] = time.time()
        if not room.devices:
            # Keep the last state for a returning device, but not the sockets.
            return
        await cls.broadcast_devices(room)
        if was_active:
            await cls.broadcast_state(room)

    @classmethod
    async def handle(cls, user_id: str, device: Device, message: dict) -> None:
        room = cls.room(user_id)
        kind = message.get("type")
        if kind == "state":
            await cls._on_state(room, device, message)
        elif kind == "claim":
            await cls._on_claim(room, device)
        elif kind == "command":
            await cls._on_command(room, device, message)

    @classmethod
    async def _on_state(cls, room: Room, device: Device, message: dict) -> None:
        async with room.lock:
            if room.active != device.id:
                # Only the active device describes the playback; a device playing with no active one takes over.
                if room.active is not None or message.get("playing") is not True:
                    return
                room.active = device.id
                devices_changed = True
            else:
                devices_changed = False
            if "queue" in message:
                queue = clean_queue(message.get("queue"))
                if queue is not None:
                    room.queue = queue
                    room.queue_rev += 1
                    # The sender already has it.
                    device.queue_rev = room.queue_rev
            index = message.get("index")
            index = index if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < max(1, len(room.queue)) else 0
            repeat = message.get("repeat") if message.get("repeat") in REPEAT_MODES else "none"
            room.state = {
                "index": index,
                "position": max(0.0, _number(message.get("position"))),
                "duration": max(0.0, _number(message.get("duration"))),
                "playing": message.get("playing") is True,
                "repeat": repeat,
                "shuffle": message.get("shuffle") is True,
                "updated": time.time(),
            }
        if devices_changed:
            await cls.broadcast_devices(room)
        await cls.broadcast_state(room, skip=device.id)

    @classmethod
    async def _on_claim(cls, room: Room, device: Device) -> None:
        async with room.lock:
            previous = room.devices.get(room.active) if room.active and room.active != device.id else None
            position = room.position_now()
            if room.state:
                room.state["position"] = position
                room.state["updated"] = time.time()
            room.active = device.id
        if previous:
            await _send(previous, {"type": "release"})
        granted = {"type": "granted", "position": position, "state": room.state, "queue_rev": room.queue_rev}
        if device.queue_rev != room.queue_rev:
            granted["queue"] = room.queue
            device.queue_rev = room.queue_rev
        await _send(device, granted)
        await cls.broadcast_devices(room)

    @classmethod
    async def _on_command(cls, room: Room, device: Device, message: dict) -> None:
        action = message.get("action")
        if action not in ACTIONS:
            return
        args = clean_args(action, message.get("args"))
        if args is None:
            return
        target = room.devices.get(room.active) if room.active else None
        if not target or target is device:
            # Nobody plays: the sender should play it itself.
            await _send(device, {"type": "no_active", "action": action, "args": args})
            return
        await _send(target, {"type": "command", "action": action, "args": args, "from": device.id})
