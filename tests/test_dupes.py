from blueshelf.dupes import SIGNATURE_VERSION, _dhash, parse_signature, similarity

MASK = 2 ** 64 - 1


def test_similarity_tolerates_re_encoding_noise():
    base = [(i * 0x9E3779B97F4A7C15) & MASK for i in range(1, 17)]
    noisy = [h ^ 0b101 for h in base]
    unrelated = [(i * 0xC2B2AE3D27D4EB4F) & MASK for i in range(1, 17)]
    assert similarity(base, noisy) == 1.0
    assert similarity(base, unrelated) < 0.6


def test_similarity_needs_enough_usable_frames():
    base = [(i * 0x9E3779B97F4A7C15) & MASK for i in range(1, 4)]
    assert similarity(base, base) == 0.0


def test_dhash_ignores_flat_frames():
    assert _dhash(bytes([10] * 72)) is None
    assert _dhash(bytes(range(72))) is not None
    assert _dhash(b"short") is None


def test_signature_roundtrip():
    assert parse_signature(f"{SIGNATURE_VERSION}:-,00000000000000ff") == [255]
    assert parse_signature("v0:00") is None
    assert parse_signature(None) is None
