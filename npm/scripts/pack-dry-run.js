"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const { packageDirectory, platforms, validate } = require("./validate-packages.js");

function pack(directory) {
  const npm = process.platform === "win32" ? "npm.cmd" : "npm";
  const result = spawnSync(npm, ["pack", "--dry-run", "--json", "--ignore-scripts"], {
    cwd: directory,
    encoding: "utf8",
  });
  if (result.status !== 0) {
    throw new Error(
      `npm pack --dry-run failed in ${directory}\n${result.stderr || result.stdout}`,
    );
  }

  let output;
  try {
    output = JSON.parse(result.stdout);
  } catch (error) {
    throw new Error(`could not parse npm pack JSON in ${directory}: ${result.stdout}`, {
      cause: error,
    });
  }
  if (!Array.isArray(output) || output.length !== 1) {
    throw new Error(`unexpected npm pack result in ${directory}`);
  }
  return output[0];
}

function fileSet(result) {
  return new Set(result.files.map(({ path: file }) => file));
}

function assertExactFiles(packageName, actual, expected) {
  const actualList = [...actual].sort();
  const expectedList = [...expected].sort();
  if (JSON.stringify(actualList) !== JSON.stringify(expectedList)) {
    throw new Error(
      `${packageName} unexpected tarball files: expected ${expectedList.join(", ")}; got ${actualList.join(", ")}`,
    );
  }
}

function main() {
  validate();

  const cliDirectory = packageDirectory("@api429/cli");
  const cliResult = pack(cliDirectory);
  const cliFiles = fileSet(cliResult);
  for (const expected of ["README.md", "bin/api429.js", "lib/launcher.js", "package.json"]) {
    if (!cliFiles.has(expected)) {
      throw new Error(`@api429/cli tarball would omit ${expected}`);
    }
  }
  assertExactFiles("@api429/cli", cliFiles, [
    "README.md",
    "bin/api429.js",
    "lib/launcher.js",
    "package.json",
  ]);
  console.log(`pack dry-run ok: ${cliResult.name}@${cliResult.version}`);

  for (const entry of platforms) {
    const directory = packageDirectory(entry.name);
    const result = pack(directory);
    const files = fileSet(result);
    const payload = `bin/${entry.binary}`;
    if (fs.existsSync(path.join(directory, payload)) && !files.has(payload)) {
      throw new Error(`${entry.name} tarball would omit ${payload}`);
    }
    if (!files.has("package.json")) {
      throw new Error(`${entry.name} tarball would omit package.json`);
    }
    const expectedFiles = ["README.md", "package.json"];
    if (fs.existsSync(path.join(directory, payload))) expectedFiles.push(payload);
    assertExactFiles(entry.name, files, expectedFiles);
    const suffix = files.has(payload) ? ` with ${payload}` : " (payload pending release staging)";
    console.log(`pack dry-run ok: ${result.name}@${result.version}${suffix}`);
  }
}

try {
  main();
} catch (error) {
  console.error(error instanceof Error ? error.message : error);
  process.exitCode = 1;
}
