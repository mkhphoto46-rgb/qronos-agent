import unittest

from core.windows_app_activation import _validate_aumid


class PackagedAppActivationTests(unittest.TestCase):
    def test_accepts_exact_packaged_application_identity(self) -> None:
        self.assertEqual(
            _validate_aumid("Microsoft.Paint_8wekyb3d8bbwe!App"),
            "Microsoft.Paint_8wekyb3d8bbwe!App",
        )

    def test_rejects_shell_or_argument_syntax(self) -> None:
        for value in (
            "",
            "Microsoft.Paint_8wekyb3d8bbwe!App --flag",
            r"shell:AppsFolder\Microsoft.Paint_8wekyb3d8bbwe!App",
            "Microsoft.Paint_8wekyb3d8bbwe!App;calc.exe",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    _validate_aumid(value)


if __name__ == "__main__":
    unittest.main()
