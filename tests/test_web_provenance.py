from __future__ import annotations

import unittest

from core.web_provenance import (
    SourceKind,
    build_strip,
    classify_source,
    registrable_host,
)


class TestRegistrableHost(unittest.TestCase):
    def test_extracts_the_host(self) -> None:
        self.assertEqual(
            registrable_host("https://docs.python.org/3/library/x.html"),
            "docs.python.org",
        )

    def test_strips_leading_www(self) -> None:
        self.assertEqual(
            registrable_host("https://www.example.com/page"),
            "example.com",
        )

    def test_lowercases(self) -> None:
        self.assertEqual(
            registrable_host("https://EXAMPLE.COM/x"),
            "example.com",
        )

    def test_accepts_a_bare_host(self) -> None:
        self.assertEqual(registrable_host("example.com/page"), "example.com")

    def test_rejects_text_that_is_not_a_url(self) -> None:
        # urlparse will happily treat arbitrary text as a hostname, which would
        # get a sentence labelled as a website.
        self.assertEqual(registrable_host("not a url at all"), "")

    def test_rejects_a_host_with_no_dot(self) -> None:
        self.assertEqual(registrable_host("localhost"), "")

    def test_rejects_empty_input(self) -> None:
        self.assertEqual(registrable_host(""), "")

    def test_persian_path_does_not_break_parsing(self) -> None:
        self.assertEqual(
            registrable_host("https://fa.wikipedia.org/wiki/پایتون"),
            "fa.wikipedia.org",
        )


class TestClassifySource(unittest.TestCase):
    def test_government_suffix_is_official(self) -> None:
        self.assertIs(
            classify_source("https://irs.gov/forms").kind,
            SourceKind.OFFICIAL,
        )

    def test_iranian_academic_suffix_is_official(self) -> None:
        self.assertIs(
            classify_source("https://sharif.ac.ir/news").kind,
            SourceKind.OFFICIAL,
        )

    def test_wikipedia_is_reference(self) -> None:
        self.assertIs(
            classify_source("https://fa.wikipedia.org/wiki/x").kind,
            SourceKind.REFERENCE,
        )

    def test_known_documentation_host_is_reference(self) -> None:
        self.assertIs(
            classify_source("https://developer.mozilla.org/en-US/x").kind,
            SourceKind.REFERENCE,
        )

    def test_docs_subdomain_is_reference_without_being_listed(self) -> None:
        # Catches the long tail without enumerating every vendor.
        self.assertIs(
            classify_source("https://docs.somevendor.io/guide").kind,
            SourceKind.REFERENCE,
        )

    def test_stackoverflow_is_community(self) -> None:
        self.assertIs(
            classify_source("https://stackoverflow.com/questions/1").kind,
            SourceKind.COMMUNITY,
        )

    def test_forum_in_the_host_is_community(self) -> None:
        self.assertIs(
            classify_source("https://forum.example.ir/thread/2").kind,
            SourceKind.COMMUNITY,
        )

    def test_an_ordinary_site_is_web(self) -> None:
        self.assertIs(
            classify_source("https://digiato.com/article/1").kind,
            SourceKind.WEB,
        )

    def test_an_unparseable_url_is_unknown(self) -> None:
        provenance = classify_source("not a url")

        self.assertIs(provenance.kind, SourceKind.UNKNOWN)
        self.assertEqual(provenance.host, "")

    def test_subject_domain_marks_a_site_official(self) -> None:
        # A question about Python can label python.org official without that
        # being hardcoded into the module.
        provenance = classify_source(
            "https://python.org/downloads",
            subject_domains=("python.org",),
        )

        self.assertIs(provenance.kind, SourceKind.OFFICIAL)

    def test_subject_domain_matches_subdomains(self) -> None:
        provenance = classify_source(
            "https://www.pypi.python.org/x",
            subject_domains=("python.org",),
        )

        self.assertIs(provenance.kind, SourceKind.OFFICIAL)

    def test_subject_domain_beats_the_other_rules(self) -> None:
        provenance = classify_source(
            "https://stackoverflow.com/x",
            subject_domains=("stackoverflow.com",),
        )

        self.assertIs(provenance.kind, SourceKind.OFFICIAL)

    def test_unrelated_subject_domain_is_ignored(self) -> None:
        provenance = classify_source(
            "https://digiato.com/x",
            subject_domains=("python.org",),
        )

        self.assertIs(provenance.kind, SourceKind.WEB)


class TestLabels(unittest.TestCase):
    def test_every_kind_has_a_persian_label(self) -> None:
        for kind in SourceKind:
            with self.subTest(kind=kind.value):
                provenance = classify_source("https://example.com")
                # label comes from the shared table, so check the table covers
                # every enum member rather than only the one classified here.
                from core.web_provenance import PERSIAN_LABEL

                self.assertIn(kind, PERSIAN_LABEL)
                self.assertTrue(PERSIAN_LABEL[kind])

                del provenance

    def test_wikipedia_label_is_marja(self) -> None:
        self.assertEqual(
            classify_source("https://fa.wikipedia.org/x").label,
            "مرجع",
        )


class TestBuildStrip(unittest.TestCase):
    def test_builds_one_entry_per_url(self) -> None:
        strip = build_strip(
            (
                "https://fa.wikipedia.org/x",
                "https://stackoverflow.com/y",
                "https://digiato.com/z",
            )
        )

        self.assertEqual(len(strip.entries), 3)

    def test_deduplicates_by_host(self) -> None:
        # Three pages from one site is one source, not three.
        strip = build_strip(
            (
                "https://digiato.com/a",
                "https://digiato.com/b",
                "https://digiato.com/c",
            )
        )

        self.assertEqual(len(strip.entries), 1)

    def test_keeps_first_appearance_order(self) -> None:
        # Reordering would imply a ranking, and the strip does not rank.
        strip = build_strip(
            (
                "https://digiato.com/a",
                "https://fa.wikipedia.org/b",
                "https://stackoverflow.com/c",
            )
        )

        self.assertEqual(
            strip.hosts,
            ("digiato.com", "fa.wikipedia.org", "stackoverflow.com"),
        )

    def test_unparseable_urls_are_dropped(self) -> None:
        strip = build_strip(("not a url", "https://example.com/x"))

        self.assertEqual(strip.hosts, ("example.com",))

    def test_empty_input_yields_an_empty_strip(self) -> None:
        strip = build_strip(())

        self.assertTrue(strip.is_empty)
        self.assertEqual(strip.render_persian(), "")

    def test_render_lists_every_host_with_its_label(self) -> None:
        strip = build_strip(
            ("https://fa.wikipedia.org/x", "https://digiato.com/y")
        )

        rendered = strip.render_persian()

        self.assertIn("fa.wikipedia.org", rendered)
        self.assertIn("مرجع", rendered)
        self.assertIn("digiato.com", rendered)
        self.assertIn("وب", rendered)

    def test_render_states_the_source_count(self) -> None:
        strip = build_strip(
            ("https://a.com/x", "https://b.com/y", "https://c.com/z")
        )

        self.assertIn("3", strip.render_persian())


if __name__ == "__main__":
    unittest.main()
