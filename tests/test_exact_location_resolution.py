from pathlib import Path

import pytest

from core.file_resolver import (
    KnownFolderFileResolver, NamedFileNotFound, NamedFileParseError,
    NamedFileRequest, NamedFileSearchLimit, NamedLocationRequest,
    ResolvedNamedFile,
    parse_named_file_request, parse_location_request,
)


def test_exact_document_does_not_scan_other_folders(tmp_path, monkeypatch):
    document = tmp_path / "گزارش.txt"
    document.write_text("test", encoding="utf-8")
    resolver = KnownFolderFileResolver(roots={})
    monkeypatch.setattr(resolver, "_scan_root", lambda *args: pytest.fail("unexpected scan"))
    request = parse_named_file_request(f'فایل "{document}" را باز کن')
    assert resolver.resolve(request).path == document


def test_explicit_folder_and_filename_limit_search_scope(tmp_path):
    document = tmp_path / "گزارش نهایی.txt"
    document.write_text("test", encoding="utf-8")
    request = parse_named_file_request(f'فایل "گزارش نهایی" داخل پوشه "{tmp_path}" را باز کن')
    assert KnownFolderFileResolver(roots={}).resolve(request).path == document


def test_exact_folder_requires_no_known_folder_registration(tmp_path):
    request = parse_location_request(f'پوشه "{tmp_path}" را باز کن')
    assert KnownFolderFileResolver(roots={}).resolve_location(request).path == tmp_path


def test_exact_executable_is_not_launched_as_a_document(tmp_path):
    target = tmp_path / "payload.exe"
    target.write_bytes(b"test")
    request = parse_named_file_request(f'فایل "{target}" را باز کن')
    with pytest.raises(NamedFileNotFound):
        KnownFolderFileResolver(roots={}).resolve(request)


def test_multiple_paths_are_not_silently_reduced_to_first(tmp_path):
    with pytest.raises(NamedFileParseError):
        parse_named_file_request(f'فایل "{tmp_path / "a.txt"}" و "{tmp_path / "b.txt"}" را باز کن')


def test_budget_expires_before_enumerating_a_large_scope(tmp_path, monkeypatch):
    clock = iter((0.0, 3.0))
    monkeypatch.setattr("core.file_resolver.time.monotonic", lambda: next(clock))
    resolver = KnownFolderFileResolver(roots={"desktop": (tmp_path,)})
    monkeypatch.setattr(resolver, "_entries", lambda *args: pytest.fail("budget already expired"))
    with pytest.raises(NamedFileSearchLimit):
        resolver.resolve(NamedFileRequest("absent", folder_hint="desktop"))


def test_exact_child_of_named_folder_does_not_scan_descendants(tmp_path, monkeypatch):
    child = tmp_path / "project"
    child.mkdir()
    resolver = KnownFolderFileResolver(roots={"desktop": (tmp_path,)})
    monkeypatch.setattr(resolver, "_scan_directories", lambda *args, **kwargs: pytest.fail("unexpected recursion"))
    assert resolver.resolve_location(NamedLocationRequest("desktop", "project")).path == child


def test_incomplete_scan_cannot_prove_even_an_exact_name_is_unique(tmp_path, monkeypatch):
    resolver = KnownFolderFileResolver(roots={"desktop": (tmp_path,)})
    hit = ResolvedNamedFile(tmp_path / "report.txt", 1000, "desktop", "report.txt")
    monkeypatch.setattr(resolver, "_scan_root", lambda *args, **kwargs: ([hit], True))
    with pytest.raises(NamedFileSearchLimit):
        resolver.resolve(NamedFileRequest("report", folder_hint="desktop"))
