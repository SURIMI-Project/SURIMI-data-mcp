"""_detect_encoding must decide UTF-8 vs Latin-1 without reading the whole file.

It used to read the entire file and decode all of it. On the 280 MB
landings CSV that is roughly 560 MB resident, and the EDITO load-data Job
runs under a 512 MiB limit, so the Job was OOM-killed (exit 137) on every
attempt after the small tables had loaded. Decoding incrementally in
chunks gives the same answer with memory bounded by the chunk size.
"""
import builtins

from surimi_data_mcp.loader.load_csv import _detect_encoding


def test_plain_utf8(tmp_path):
    p = tmp_path / "a.csv"; p.write_bytes("name,value\nsardine,1\nanchois,2\n".encode("utf-8"))
    assert _detect_encoding(str(p)) == "utf-8"


def test_a_latin1_byte_falls_back(tmp_path):
    p = tmp_path / "b.csv"; p.write_bytes(b"name\ncaf\xe9\n")
    assert _detect_encoding(str(p)) == "latin-1"


def test_a_multibyte_char_split_across_a_chunk_boundary_is_still_utf8(tmp_path):
    """A naive per-chunk decode would call this Latin-1 and mangle every accent."""
    body = "x" * 1023 + "é" + "y" * 10          # é is 2 bytes: offsets 1023 and 1024
    p = tmp_path / "c.csv"; p.write_bytes(body.encode("utf-8"))
    assert _detect_encoding(str(p), chunk_size=1024) == "utf-8"


def test_a_truncated_multibyte_char_at_eof_falls_back(tmp_path):
    p = tmp_path / "d.csv"; p.write_bytes(b"ok\n\xc3")   # lone lead byte, nothing after
    assert _detect_encoding(str(p)) == "latin-1"


def test_it_never_reads_the_whole_file(tmp_path, monkeypatch):
    p = tmp_path / "e.csv"; p.write_bytes(b"a,b\n" * 5000)
    sizes = []
    real_open = builtins.open

    def spy_open(*a, **k):
        f = real_open(*a, **k)
        read = f.read
        def spying(n=-1):
            sizes.append(n)
            return read(n)
        f.read = spying
        return f

    monkeypatch.setattr(builtins, "open", spy_open)
    assert _detect_encoding(str(p), chunk_size=4096) == "utf-8"
    assert sizes and all(n == 4096 for n in sizes), sizes
