"""Tests de la galerie (sans caméra) : python -m pytest host/faces"""
import numpy as np

from gallery import Gallery


def unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def person_vectors(seed, n=3, noise=0.15):
    rng = np.random.default_rng(seed)
    base = unit(rng.normal(size=128))
    return base, [unit(base + noise * unit(rng.normal(size=128))) for _ in range(n)]


def test_recognizes_enrolled_person_and_rejects_stranger(tmp_path):
    g = Gallery(tmp_path / "g.npz")
    alice, alice_refs = person_vectors(1)
    _, bob_refs = person_vectors(2)
    for v in alice_refs:
        g.add(1, "Alice", "ADMIN", v)
    for v in bob_refs:
        g.add(2, "Bob", "LECTEUR", v)
    pid, name, role, score = g.match(unit(alice + 0.15 * unit(np.random.default_rng(9).normal(size=128))))
    assert (pid, name, role) == (1, "Alice", "ADMIN") and score > 0.6
    stranger = unit(np.random.default_rng(42).normal(size=128))
    assert g.match(stranger)[0] is None


def test_empty_gallery_recognizes_nobody(tmp_path):
    assert Gallery(tmp_path / "absent.npz").match(np.ones(128))[0] is None


def test_save_reload_and_change_detection(tmp_path):
    path = tmp_path / "g.npz"
    g = Gallery(path)
    _, refs = person_vectors(3)
    g.add(5, "Chloé", "OPERATEUR", refs[0])
    other = Gallery(path)
    assert other.people() == {5: ("Chloé", "OPERATEUR", 1)}
    g.add(5, "Chloé", "OPERATEUR", refs[1])
    assert other.reload_if_changed() and other.people()[5][2] == 2
    g.remove(5)
    assert other.reload_if_changed() and len(other) == 0
