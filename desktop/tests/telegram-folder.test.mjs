import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import nodeTest from "node:test";
import vm from "node:vm";
import ts from "typescript";

const test = (name, fn) => nodeTest(name, { timeout: 5000 }, fn);

function source(relative) {
  return readFileSync(new URL(relative, import.meta.url), "utf8");
}

// The real module, compiled exactly as shipped, run in an isolated context with no imports.
function load(relative) {
  const compiled = ts.transpileModule(source(relative), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const exports = {};
  vm.runInNewContext(compiled, {
    exports,
    require(name) { throw new Error(`Unexpected import: ${name}`); },
    RegExp, String, Array,
  }, { timeout: 1000 });
  return exports;
}

const folder = load("../src/telegramFolder.ts");

test("the key is the backend's setting and an empty value clears it", () => {
  assert.equal(folder.TELEGRAM_FOLDER_KEY, "workflows.telegram_download_folder");
  assert.equal(folder.telegramFolderProblem(""), null);
  assert.equal(folder.telegramFolderProblem("   "), null);
});

test("only an absolute local drive path is offered for saving (the backend checks again)", () => {
  for (const good of ["D:\\Downloads\\Telegram Desktop", "C:/Users/me/Downloads", "e:\\x"]) {
    assert.equal(folder.telegramFolderProblem(good), null, good);
  }
  const bad = {
    "relative\\downloads": "مسیر کامل",
    "\\\\server\\share": "شبکه",
    "\\\\?\\C:\\dl": "شبکه",
    "//server/share": "شبکه",
    "C:\\dl\\..\\x": "مسیر کامل",
    "C:\\a\u0007b": "نویسهٔ نامعتبر",
    ["C:" + "\\x".repeat(600)]: "طولانی",
  };
  for (const [value, word] of Object.entries(bad)) {
    const problem = folder.telegramFolderProblem(value);
    assert.ok(problem && problem.includes(word), `${JSON.stringify(value)} -> ${problem}`);
  }
});

test("the status never claims the folder works before a download checks it", () => {
  assert.match(folder.telegramFolderStatus(""), /تعیین نشده/);
  assert.match(folder.telegramFolderStatus("D:\\x"), /پیش از هر دانلود دوباره بررسی می‌شود/);
});

test("the settings view saves through the backend and asks the runtime to reload", () => {
  const view = source("../src/components/SettingsView.tsx");
  assert.ok(view.includes("await setSetting(TELEGRAM_FOLDER_KEY, value)"));
  assert.ok(view.includes("<TelegramFolderSetting"));
  const component = source("../src/components/TelegramFolderSetting.tsx");
  assert.ok(component.includes("telegramFolderProblem(value)") && !component.includes("open(")); // typed, no dialog
});
