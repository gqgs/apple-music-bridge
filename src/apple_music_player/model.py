from dataclasses import dataclass
from hashlib import sha256
from urllib.parse import parse_qs, urlparse


class PlayerError(Exception):
    """Only fixed, safe-to-display messages cross the worker/UI boundary."""


@dataclass(frozen=True)
class Album:
    country: str
    id: str
    title: str
    artist: str = ""
    artist_id: str = ""
    album_title: str = ""
    artwork_url: str = ""
    release_date: str = ""
    store_url: str = ""

    @property
    def url(self):
        return f"https://itunes.apple.com/{self.country}/album/id{self.id}"


@dataclass(frozen=True)
class Track:
    country: str
    id: str
    title: str
    album_id: str = ""
    artist: str = ""
    artist_id: str = ""
    album_title: str = ""
    artwork_url: str = ""
    release_date: str = ""
    store_url: str = ""

    track_name: str = ""
    album_artist: str = ""
    album_artist_id: str = ""
    track_number: int = 0
    disc_number: int = 0
    duration_ms: int = 0

    @property
    def duration_label(self):
        if not self.duration_ms:
            return "—"
        seconds = self.duration_ms // 1000
        if seconds >= 3600:
            return f"{seconds // 3600}:{seconds // 60 % 60:02}:{seconds % 60:02}"
        return f"{seconds // 60}:{seconds % 60:02}"

    def album_row_title(self, multiple_discs=False):
        number = f"{self.track_number:02}" if self.track_number else "—"
        if multiple_discs:
            number = f"{self.disc_number or '—'}.{number}"
        title = self.track_name or self.title.removesuffix(" — " + self.artist)
        artist = self.album_artist or self.artist
        return f"{number}  {title}" + (f" — {artist}" if artist else "")

    @property
    def album(self):
        if not self.album_id:
            return None
        return Album(
            self.country,
            self.album_id,
            f"{self.album_title or 'Album ' + self.album_id} — {self.album_artist or self.artist}",
            self.album_artist or self.artist,
            self.album_artist_id
            or (
                self.artist_id if not self.album_artist or self.album_artist == self.artist else ""
            ),
            self.album_title,
            self.artwork_url,
            self.release_date,
            self.store_url,
        )

    @property
    def url(self):
        return f"https://music.apple.com/{self.country}/song/{self.id}"

    def key(self, quality):
        return sha256(f"{self.country}:{self.id}:{quality}".encode()).hexdigest()


def parse_url(value):
    p = urlparse(value.strip())
    bits = p.path.strip("/").split("/")
    if (
        p.scheme != "https"
        or p.netloc != "music.apple.com"
        or len(bits) < 3
        or len(bits[0]) != 2
        or not bits[0].isascii()
        or not bits[0].isalpha()
        or bits[1] not in {"album", "song"}
    ):
        raise PlayerError("Paste an https://music.apple.com country/album or country/song URL.")
    item = parse_qs(p.query).get("i", [bits[-1].removeprefix("id")])[0]
    if not item.isascii() or not item.isdecimal():
        raise PlayerError("The URL must contain a numeric song or album ID.")
    kind = "song" if "i" in parse_qs(p.query) else bits[1]
    return bits[0].lower(), kind, item


@dataclass(frozen=True)
class Media:
    codec: str
    rate: int = 0
    bits: int = 0
    channels: int = 0
    atmos: bool = False

    @classmethod
    def from_probe(cls, data):
        streams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
        if len(streams) != 1:
            raise PlayerError("Downloaded file must have exactly one readable audio stream.")
        s = streams[0]
        codec = s.get("codec_name", "")
        if codec not in {"alac", "flac", "aac", "ac3", "eac3"}:
            raise PlayerError("Downloaded audio codec is unsupported or unreadable.")
        # ffprobe's demuxer/decoder profile, never a user-editable tag or catalog badge.
        joc = codec == "eac3" and s.get("profile") in {
            "Dolby Digital Plus + Dolby Atmos",
            "E-AC-3 JOC",
        }
        return cls(
            codec,
            int(s.get("sample_rate", 0)),
            int(s.get("bits_per_raw_sample", 0) or 0),
            int(s.get("channels", 0)),
            joc,
        )

    @property
    def label(self):
        if self.atmos:
            kind = "Dolby Atmos (JOC detected)"
        elif self.codec in {"alac", "flac"}:
            kind = "Lossless"
        elif self.codec == "eac3":
            kind = "Lossy · Dolby Digital Plus / Atmos unverified"
        else:
            kind = "Lossy"
        detail = [self.codec.upper()]
        if self.bits:
            detail.append(f"{self.bits}-bit")
        if self.rate:
            detail.append(f"{self.rate / 1000:g} kHz")
        if self.channels:
            detail.append(f"{self.channels} ch")
        return f"{kind} · " + " / ".join(detail)

    def warning(self, requested):
        if requested == "atmos" and not self.atmos:
            return "Requested Atmos; " + (
                "JOC is unverified."
                if self.codec == "eac3"
                else "downloaded a different format (fallback)."
            )
        if requested == "lossless" and self.codec not in {"alac", "flac"}:
            return "Requested lossless; downloaded lossy audio (fallback)."
        return ""
