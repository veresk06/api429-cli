"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const test = require("node:test");

const { expectedTarballFilename, isInside } = require("./pack-release.js");
const { platforms, validatePayload } = require("./validate-packages.js");

function writeMachO(file, arch) {
  const value = Buffer.alloc(48);
  value.set([0xcf, 0xfa, 0xed, 0xfe], 0);
  value.writeUInt32LE(arch === "x64" ? 0x01000007 : 0x0100000c, 4);
  value.writeUInt32LE(2, 12);
  value.writeUInt32LE(1, 16);
  value.writeUInt32LE(16, 20);
  value.writeUInt32LE(0x24, 32);
  value.writeUInt32LE(16, 36);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, value, { mode: 0o755 });
}

function writeElf(file, arch) {
  const interpreter = Buffer.from(
    arch === "x64"
      ? "/lib64/ld-linux-x86-64.so.2\0"
      : "/lib/ld-linux-aarch64.so.1\0",
  );
  const value = Buffer.alloc(176 + interpreter.length);
  value.set([0x7f, 0x45, 0x4c, 0x46, 2, 1, 1], 0);
  value.writeUInt16LE(3, 16);
  value.writeUInt16LE(arch === "x64" ? 0x3e : 0xb7, 18);
  value.writeUInt32LE(1, 20);
  value.writeBigUInt64LE(0x1000n, 24);
  value.writeBigUInt64LE(64n, 32);
  value.writeUInt16LE(64, 52);
  value.writeUInt16LE(56, 54);
  value.writeUInt16LE(2, 56);
  value.writeUInt32LE(1, 64);
  value.writeBigUInt64LE(BigInt(value.length), 96);
  value.writeBigUInt64LE(BigInt(value.length), 104);
  value.writeUInt32LE(3, 120);
  value.writeBigUInt64LE(176n, 128);
  value.writeBigUInt64LE(BigInt(interpreter.length), 152);
  interpreter.copy(value, 176);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, value, { mode: 0o755 });
}

function writePe(file, arch) {
  const value = Buffer.alloc(240);
  value.set([0x4d, 0x5a], 0);
  value.writeUInt32LE(64, 0x3c);
  value.set([0x50, 0x45, 0x00, 0x00], 64);
  value.writeUInt16LE(arch === "x64" ? 0x8664 : 0xaa64, 68);
  value.writeUInt16LE(1, 70);
  value.writeUInt16LE(112, 84);
  value.writeUInt16LE(0x0002, 86);
  value.writeUInt16LE(0x020b, 88);
  value.writeUInt32LE(0x1000, 104);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, value);
}

test("release helper derives safe scoped tarball names and detects nested paths", () => {
  assert.equal(
    expectedTarballFilename("@api429/cli-linux-x64-gnu", "0.1.0"),
    "api429-cli-linux-x64-gnu-0.1.0.tgz",
  );
  assert.equal(isInside(path.join("a", "package"), path.join("a", "package", "dist")), true);
  assert.equal(isInside(path.join("a", "package"), path.join("a", "release")), false);
});

test("native validation rejects header-only Mach-O, ELF, and PE stubs", () => {
  const temporaryRoot = fs.mkdtempSync(path.join(os.tmpdir(), "api429-invalid-payload-"));
  try {
    const machO = path.join(temporaryRoot, "api429-macho");
    const machHeader = Buffer.alloc(32);
    machHeader.set([0xcf, 0xfa, 0xed, 0xfe], 0);
    machHeader.writeUInt32LE(0x0100000c, 4);
    fs.writeFileSync(machO, machHeader, { mode: 0o755 });
    assert.throws(
      () => validatePayload(platforms.find(({ name }) => name === "@api429/cli-darwin-arm64"), machO),
      /MH_EXECUTE/,
    );

    const elf = path.join(temporaryRoot, "api429-elf");
    const elfHeader = Buffer.alloc(64);
    elfHeader.set([0x7f, 0x45, 0x4c, 0x46, 2, 1, 1], 0);
    elfHeader.writeUInt16LE(0x3e, 18);
    fs.writeFileSync(elf, elfHeader, { mode: 0o755 });
    assert.throws(
      () => validatePayload(platforms.find(({ name }) => name === "@api429/cli-linux-x64-gnu"), elf),
      /ET_EXEC or ET_DYN/,
    );

    const pe = path.join(temporaryRoot, "api429.exe");
    const peHeader = Buffer.alloc(240);
    peHeader.set([0x4d, 0x5a], 0);
    peHeader.writeUInt32LE(64, 0x3c);
    peHeader.set([0x50, 0x45, 0x00, 0x00], 64);
    peHeader.writeUInt16LE(0x8664, 68);
    peHeader.writeUInt16LE(1, 70);
    peHeader.writeUInt16LE(112, 84);
    fs.writeFileSync(pe, peHeader);
    assert.throws(
      () => validatePayload(platforms.find(({ name }) => name === "@api429/cli-win32-x64"), pe),
      /PE executable image/,
    );
  } finally {
    fs.rmSync(temporaryRoot, { force: true, recursive: true });
  }
});

test(
  "pack-release creates six platform tarballs before the meta tarball and emits checksums",
  { skip: process.platform === "win32", timeout: 30_000 },
  () => {
    const temporaryBase = fs.mkdtempSync(path.join(os.tmpdir(), "api429-pack-release-"));
    try {
      const sourceRoot = path.resolve(__dirname, "..");
      const npmRoot = path.join(temporaryBase, "npm");
      fs.cpSync(sourceRoot, npmRoot, {
        filter(source) {
          return path.basename(source) !== "node_modules" && !source.endsWith(".tgz");
        },
        recursive: true,
      });
      fs.writeFileSync(
        path.join(temporaryBase, "pyproject.toml"),
        '[project]\nname = "api429-cli"\nversion = "0.1.0"\n',
      );

      writeMachO(path.join(npmRoot, "packages/cli-darwin-arm64/bin/api429"), "arm64");
      writeMachO(path.join(npmRoot, "packages/cli-darwin-x64/bin/api429"), "x64");
      writeElf(path.join(npmRoot, "packages/cli-linux-arm64-gnu/bin/api429"), "arm64");
      writeElf(path.join(npmRoot, "packages/cli-linux-x64-gnu/bin/api429"), "x64");
      writePe(path.join(npmRoot, "packages/cli-win32-arm64/bin/api429.exe"), "arm64");
      writePe(path.join(npmRoot, "packages/cli-win32-x64/bin/api429.exe"), "x64");

      const outputDirectory = path.join(temporaryBase, "release");
      const npm = process.platform === "win32" ? "npm.cmd" : "npm";
      const result = spawnSync(
        npm,
        ["run", "--silent", "pack:release", "--", outputDirectory],
        { cwd: npmRoot, encoding: "utf8" },
      );
      assert.equal(result.status, 0, result.stderr || result.stdout);
      const manifest = JSON.parse(result.stdout);
      assert.equal(manifest.schemaVersion, 1);
      assert.equal(manifest.version, "0.1.0");
      assert.equal(manifest.packages.length, 7);
      assert.equal(manifest.packages.at(-1).name, "@api429/cli");
      assert.equal(manifest.packages.slice(0, -1).every(({ name }) => name !== "@api429/cli"), true);

      for (const entry of manifest.packages) {
        assert.equal(path.dirname(entry.tarball), outputDirectory);
        assert.equal(fs.existsSync(entry.tarball), true);
        const digest = crypto
          .createHash("sha512")
          .update(fs.readFileSync(entry.tarball))
          .digest("hex");
        assert.equal(entry.sha512, digest);
      }
    } finally {
      fs.rmSync(temporaryBase, { force: true, recursive: true });
    }
  },
);
