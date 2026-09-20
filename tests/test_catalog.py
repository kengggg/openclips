import json

from openclips.catalog import CATALOG_NAME, Catalog


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


def test_ignores_garbage_file(tmp_path):
    (tmp_path / CATALOG_NAME).write_text(json.dumps([1, 2]))
    assert Catalog.for_dir(tmp_path).sessions() == {}


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
