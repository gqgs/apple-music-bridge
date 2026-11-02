"""Source-reviewed contracts and upstream-published artifact identities (2026-11-02)."""

from dataclasses import dataclass

CLASSIC_DOWNLOADER = "12fff0caead84c264abdb02878d43b1773e13368"
CLASSIC_WRAPPER = "4459548fb3c0f96dcb974755eb4a5a0791fbbc74"
LITE_WRAPPER = "56a7dcd726f2c2ef1d4420d6747ba0bafe215fee"
LITE_DOWNLOADER = "452ba0b672d49afffca3f57b0c2c4670a316e2ca"
LITE_ORIGIN = "https://github.com/WorldObservationLog/wrapper/actions/runs/41407950505"
LITE_ARTIFACT_ID = 13124456520
LITE_ARTIFACT_NAME = "wrapper-lite-linux-x86_64"
LITE_ARTIFACT_EXPIRES = "2027-01-31"
LITE_ENDPOINT = "http://127.0.0.1:12340"


@dataclass(frozen=True)
class RuntimePin:
    wrapper_sha256: str
    downloader_sha256: str
    downloader_url: str


PIN = RuntimePin(
    "98d6a0d8c5118ce96b97abb0561623aa87feb771b258d280dd52cfa8a21f2c7f",
    "1a3a4000edd74be98efccf7f611b97d47ef822d8e059805c6aaaef98f0477747",
    "https://github.com/zhaarey/apple-music-downloader/releases/download/v0.1.4/amdl_linux_amd64",
)
