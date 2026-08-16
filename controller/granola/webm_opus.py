"""WebM (Matroska) -> Ogg Opus, in pure Python.

The Move has no ffmpeg and cannot get one without shipping a large binary, but its
libsndfile (1.0.31) reads **Ogg Opus** natively — and YouTube's best audio-only stream
(itag 251) is already Opus, just wrapped in WebM. So nothing needs decoding or
re-encoding: the Opus packets are lifted out of the Matroska clusters and re-framed into
Ogg pages. Sample-for-sample the original bitstream, at no CPU cost worth measuring.

Only what that job needs is implemented — enough EBML to find the Opus track and walk its
blocks, and enough Ogg to write a valid stream. A truncated input is fine and expected:
harvesting downloads a prefix of a long video, so the last cluster is usually incomplete
and simply stops contributing packets.
"""
from __future__ import annotations

import struct

# --- EBML ---------------------------------------------------------------------
ID_SEGMENT = 0x18538067
ID_TRACKS = 0x1654AE6B
ID_TRACK_ENTRY = 0xAE
ID_TRACK_NUMBER = 0xD7
ID_CODEC_ID = 0x86
ID_CODEC_PRIVATE = 0x63A2
ID_AUDIO = 0xE1
ID_SAMPLING_FREQ = 0xB5
ID_CHANNELS = 0x9F
ID_CLUSTER = 0x1F43B675
ID_SIMPLE_BLOCK = 0xA3
ID_BLOCK_GROUP = 0xA0
ID_BLOCK = 0xA1
# Containers we descend into; everything else is skipped whole.
MASTERS = {ID_SEGMENT, ID_TRACKS, ID_TRACK_ENTRY, ID_AUDIO, ID_CLUSTER, ID_BLOCK_GROUP}


class Truncated(Exception):
    """The input stops mid-element — expected when only a prefix was downloaded."""


def _read_id(buf: memoryview, pos: int) -> tuple[int, int]:
    if pos >= len(buf):
        raise Truncated
    first = buf[pos]
    length = 1 if first & 0x80 else 2 if first & 0x40 else 3 if first & 0x20 else 4 if first & 0x10 else 0
    if length == 0 or pos + length > len(buf):
        raise Truncated
    val = 0
    for i in range(length):
        val = (val << 8) | buf[pos + i]
    return val, pos + length          # IDs keep their marker bits


def _read_size(buf: memoryview, pos: int) -> tuple[int | None, int]:
    if pos >= len(buf):
        raise Truncated
    first = buf[pos]
    if first == 0:
        raise Truncated
    length = 1
    mask = 0x80
    while not (first & mask):
        mask >>= 1
        length += 1
    if pos + length > len(buf):
        raise Truncated
    val = first & (mask - 1)
    unknown = val == mask - 1
    for i in range(1, length):
        val = (val << 8) | buf[pos + i]
        unknown = unknown and buf[pos + i] == 0xFF
    # "Unknown size" is legal for Segment and for live-muxed Clusters: descend anyway.
    return (None if unknown else val), pos + length


def _uint(data: memoryview) -> int:
    v = 0
    for b in data:
        v = (v << 8) | b
    return v


def _parse_track_entry(buf: memoryview, start: int, end: int) -> dict:
    """One TrackEntry, flat. Parsed by its own function rather than by the generic walker
    because its fields have to be collected TOGETHER — a recursive walk puts each child in
    the callee's locals, where the caller deciding "is this the Opus track?" cannot see
    them. That mistake made a WebM full of Opus report no Opus track at all.
    """
    info: dict = {}
    pos = start
    while pos < end:
        try:
            eid, pos = _read_id(buf, pos)
            size, pos = _read_size(buf, pos)
        except Truncated:
            break
        if size is None:
            size = end - pos
        stop = min(pos + size, end)
        if eid == ID_TRACK_NUMBER:
            info["number"] = _uint(buf[pos:stop])
        elif eid == ID_CODEC_ID:
            info["codec"] = bytes(buf[pos:stop]).rstrip(b"\0").decode("ascii", "replace")
        elif eid == ID_CODEC_PRIVATE:
            info["private"] = bytes(buf[pos:stop])
        elif eid == ID_AUDIO:
            info.update(_parse_audio(buf, pos, stop))
        pos = stop
    return info


def _parse_audio(buf: memoryview, start: int, end: int) -> dict:
    out: dict = {}
    pos = start
    while pos < end:
        try:
            eid, pos = _read_id(buf, pos)
            size, pos = _read_size(buf, pos)
        except Truncated:
            break
        if size is None:
            size = end - pos
        stop = min(pos + size, end)
        if eid == ID_CHANNELS:
            out["channels"] = _uint(buf[pos:stop])
        elif eid == ID_SAMPLING_FREQ:
            if stop - pos == 4:
                out["rate"] = int(struct.unpack(">f", bytes(buf[pos:stop]))[0])
            elif stop - pos == 8:
                out["rate"] = int(struct.unpack(">d", bytes(buf[pos:stop]))[0])
        pos = stop
    return out


def parse_webm_opus(data: bytes) -> tuple[bytes, int, int, list[bytes]]:
    """Return (OpusHead, channels, sample_rate, opus_packets) from a WebM byte string."""
    buf = memoryview(data)
    state = {"head": None, "track": None, "channels": 2, "rate": 48000}
    packets: list[bytes] = []

    def walk(start: int, end: int) -> None:
        pos = start
        while pos < end:
            try:
                eid, pos = _read_id(buf, pos)
                size, pos = _read_size(buf, pos)
            except Truncated:
                return
            if size is None:                       # unknown length: runs to the parent's end
                size = end - pos
            stop = min(pos + size, end)
            if eid == ID_TRACK_ENTRY:
                t = _parse_track_entry(buf, pos, stop)
                if t.get("codec") == "A_OPUS" and state["track"] is None:
                    state["track"] = t.get("number")
                    state["head"] = t.get("private")
                    state["channels"] = t.get("channels", state["channels"])
                    state["rate"] = t.get("rate", state["rate"])
            elif eid in MASTERS:
                walk(pos, stop)
            elif eid in (ID_SIMPLE_BLOCK, ID_BLOCK):
                if state["track"] is not None and stop - pos > 4:
                    _extract_block(buf[pos:stop], state["track"], packets)
            pos = stop
            if pos > end:
                return

    walk(0, len(buf))
    if state["head"] is None:
        raise ValueError("no Opus track in this WebM")
    return state["head"], int(state["channels"]), int(state["rate"]), packets


def _extract_block(block: memoryview, want_track: int, out: list[bytes]) -> None:
    """One SimpleBlock/Block: track number (vint), 16-bit timecode, flags, then frames."""
    try:
        num, pos = _read_size(block, 0)
    except Truncated:
        return
    if num != want_track or pos + 3 > len(block):
        return
    flags = block[pos + 2]
    pos += 3
    lacing = (flags >> 1) & 0x03
    if lacing == 0:                                # no lacing: the rest is one frame
        if pos < len(block):
            out.append(bytes(block[pos:]))
        return
    if pos >= len(block):
        return
    count = block[pos] + 1
    pos += 1
    sizes: list[int] = []
    if lacing == 2:                                # fixed-size lacing
        rest = len(block) - pos
        if count <= 0 or rest % count:
            return
        sizes = [rest // count] * count
    elif lacing == 1:                              # Xiph lacing
        for _ in range(count - 1):
            n = 0
            while pos < len(block) and block[pos] == 255:
                n += 255
                pos += 1
            if pos >= len(block):
                return
            n += block[pos]
            pos += 1
            sizes.append(n)
        sizes.append(len(block) - pos - sum(sizes))
    else:                                          # EBML lacing
        try:
            first, pos = _read_size(block, pos)
        except Truncated:
            return
        sizes.append(first)
        for _ in range(count - 2):
            try:
                delta_raw, pos = _read_size(block, pos)
            except Truncated:
                return
            # signed vint: bias by half the range of its own width
            width = 1
            probe = delta_raw
            while probe >= (1 << (7 * width)) - 1 and width < 8:
                width += 1
            sizes.append(sizes[-1] + delta_raw - ((1 << (7 * width - 1)) - 1))
        sizes.append(len(block) - pos - sum(sizes))
    for n in sizes:
        if n <= 0 or pos + n > len(block):
            return
        out.append(bytes(block[pos:pos + n]))
        pos += n


# --- Opus packet duration ------------------------------------------------------
def opus_samples(packet: bytes) -> int:
    """Duration of one Opus packet in 48 kHz samples, from its TOC byte (RFC 6716 §3.1)."""
    if not packet:
        return 0
    toc = packet[0]
    config = toc >> 3
    if config < 12:
        ms = (10, 20, 40, 60)[config & 3]
    elif config < 16:
        ms = (10, 20)[config & 1]
    else:
        ms = (2.5, 5, 10, 20)[config & 3]
    code = toc & 3
    if code == 0:
        frames = 1
    elif code in (1, 2):
        frames = 2
    else:
        frames = (packet[1] & 0x3F) if len(packet) > 1 else 1
    return int(round(ms * 48 * frames))


# --- Ogg -----------------------------------------------------------------------
def _crc_table() -> list[int]:
    table = []
    for i in range(256):
        r = i << 24
        for _ in range(8):
            r = ((r << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if r & 0x80000000 else (r << 1) & 0xFFFFFFFF
        table.append(r)
    return table


_CRC = _crc_table()


def _crc32(data: bytes) -> int:
    # Ogg's CRC is the unreflected 0x04C11DB7 variant with zero init and no final xor —
    # not zlib's.
    r = 0
    for b in data:
        r = ((r << 8) & 0xFFFFFFFF) ^ _CRC[((r >> 24) & 0xFF) ^ b]
    return r


def _page(serial: int, seq: int, granule: int, packets: list[bytes], flags: int) -> bytes:
    lacing = bytearray()
    for p in packets:
        n = len(p)
        while n >= 255:
            lacing.append(255)
            n -= 255
        lacing.append(n)
    body = b"".join(packets)
    header = bytearray(b"OggS")
    header.append(0)
    header.append(flags)
    header += struct.pack("<q", granule)
    header += struct.pack("<I", serial)
    header += struct.pack("<I", seq)
    header += b"\0\0\0\0"                          # CRC placeholder
    header.append(len(lacing))
    header += lacing
    page = bytes(header) + body
    crc = _crc32(page)
    return page[:22] + struct.pack("<I", crc) + page[26:]


def write_ogg_opus(path: str, head: bytes, packets: list[bytes], serial: int = 0x4752414E) -> int:
    """Write the packets as an Ogg Opus file. Returns the number of 48 kHz samples."""
    out = bytearray()
    seq = 0
    out += _page(serial, seq, 0, [head], 0x02)     # BOS
    seq += 1
    tags = b"OpusTags" + struct.pack("<I", 7) + b"granola" + struct.pack("<I", 0)
    out += _page(serial, seq, 0, [tags], 0x00)
    seq += 1

    granule = 0
    batch: list[bytes] = []
    for p in packets:
        batch.append(p)
        granule += opus_samples(p)
        # A page carries at most 255 lacing values; keep well under and flush often so a
        # truncated file still decodes right up to its end.
        if sum(1 + len(x) // 255 for x in batch) >= 200:
            out += _page(serial, seq, granule, batch, 0x00)
            seq += 1
            batch = []
    out += _page(serial, seq, granule, batch or [b""], 0x04)   # EOS
    with open(path, "wb") as f:
        f.write(bytes(out))
    return granule


def remux(webm: bytes, path: str) -> dict:
    """WebM bytes -> an Ogg Opus file. Returns what was found, for logging."""
    head, channels, rate, packets = parse_webm_opus(webm)
    if not packets:
        raise ValueError("no Opus packets found")
    samples = write_ogg_opus(path, head, packets)
    return {"channels": channels, "rate": rate, "packets": len(packets),
            "samples": samples, "seconds": round(samples / 48000.0, 2)}
