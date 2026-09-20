import json

import pytest

from openclips.catalog import CATALOG_NAME, Catalog
from openclips.errors import StorageError


def test_roundtrip(tmp_path):
    cat = Catalog.for_dir(tmp_path)
    assert cat.path == tmp_path / CATALOG_NAME and not cat.has(1, 1)
    f = tmp_path / "1" / "moment_1.jpg"
    f.parent.mkdir()
    f.write_bytes(b"x")
    cat.record(1, 1, f, 1, timestamp_ms=5, score=0.5, camera="AA")
    cat.save()
    cat2 = Catalog.for_dir(tmp_path)
    assert cat2.has(1, 1) and cat2.moments(1)[1]["score"] == 0.5 and cat2.sessions()[1]["camera"] == "AA"
    f.unlink()
    assert not cat2.has(1, 1)  # file gone -> not "have"
    assert cat2.get(1, 1) is not None
    assert cat2.forget(1, 1) and not cat2.forget(1, 1)


def test_malformed_catalog_is_preserved(tmp_path):
    path = tmp_path / CATALOG_NAME
    path.write_text(json.dumps([1, 2]))
    with pytest.raises(StorageError, match="invalid"):
        Catalog.for_dir(tmp_path)
    assert json.loads(path.read_text()) == [1, 2]
    cat = Catalog.rebuild(path)
    assert cat.sessions() == {}
    assert (tmp_path / (CATALOG_NAME + ".invalid")).exists()


def test_reads_old_entries_without_resolution(tmp_path):
    f = tmp_path / "1" / "moment_1.jpg"
    f.parent.mkdir()
    f.write_bytes(b"x")
    cat = Catalog.for_dir(tmp_path)
    cat.record(1, 1, f, 1)
    cat.save()
    loaded = Catalog.for_dir(tmp_path).get(1, 1)
    assert loaded["size"] == 1 and "resolution" not in loaded
    cat.record(1, 1, f, 1, resolution=1)
    cat.save()
    assert Catalog.for_dir(tmp_path).get(1, 1)["resolution"] == 1


def test_session_collision_rejected(tmp_path):
    cat = Catalog.for_dir(tmp_path)
    f = tmp_path / "1" / "moment_1.jpg"
    f.parent.mkdir()
    f.write_bytes(b"x")
    cat.record(1, 1, f, 1, camera="AA:BB:CC:DD:EE:FF")
    with pytest.raises(StorageError, match="collision"):
        cat.record(1, 2, f, 1, camera="11:22:33:44:55:66")
