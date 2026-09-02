"use strict";

const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const test = require("node:test");

const {
  detectLinuxLibc,
  launch,
  resolveBinary,
  selectTarget,
  signalExitCode,
} = require("../lib/launcher.js");

function fakeProcess(overrides = {}) {
  const value = new EventEmitter();
  return Object.assign(
    value,
    {
      arch: "x64",
      argv: ["node", "api429", "--version"],
      exitCode: undefined,
      kill() {},
      pid: 4242,
      platform: "darwin",
      stderr: { write() {} },
    },
    overrides,
  );
}

function fakeChild() {
  const value = new EventEmitter();
  value.kill = () => true;
  return value;
}

test("selectTarget maps every supported platform and architecture", () => {
  const cases = [
    ["darwin", "x64", "unknown", "@api429/cli-darwin-x64", "api429"],
    ["darwin", "arm64", "unknown", "@api429/cli-darwin-arm64", "api429"],
    ["linux", "x64", "glibc", "@api429/cli-linux-x64-gnu", "api429"],
    ["linux", "arm64", "glibc", "@api429/cli-linux-arm64-gnu", "api429"],
    ["win32", "x64", "unknown", "@api429/cli-win32-x64", "api429.exe"],
    ["win32", "arm64", "unknown", "@api429/cli-win32-arm64", "api429.exe"],
  ];

  for (const [platform, arch, libc, packageName, binaryName] of cases) {
    assert.deepEqual(selectTarget({ platform, arch, libc }), {
      binaryName,
      packageName,
      target:
        platform === "linux" ? `${platform}-${arch}-gnu` : `${platform}-${arch}`,
    });
  }
});

test("selectTarget explains unsupported platforms, architectures, and musl", () => {
  assert.throws(
    () => selectTarget({ platform: "freebsd", arch: "x64" }),
    /unsupported platform: freebsd-x64/,
  );
  assert.throws(
    () => selectTarget({ platform: "darwin", arch: "ia32" }),
    /unsupported platform: darwin-ia32/,
  );
  assert.throws(
    () => selectTarget({ platform: "linux", arch: "x64", libc: "musl" }),
    /require glibc/,
  );
});

test("detectLinuxLibc distinguishes glibc, musl, and restricted reports", () => {
  assert.equal(
    detectLinuxLibc({ getReport: () => ({ header: { glibcVersionRuntime: "2.39" } }) }),
    "glibc",
  );
  assert.equal(
    detectLinuxLibc({ getReport: () => ({ sharedObjects: ["/lib/ld-musl-x86_64.so.1"] }) }),
    "musl",
  );
  assert.equal(detectLinuxLibc({ getReport: () => { throw new Error("disabled"); } }), "unknown");
});

test("resolveBinary resolves the executable relative to the platform package", () => {
  const target = selectTarget({ platform: "win32", arch: "arm64" });
  const packageJson = path.join("root", "node_modules", "@api429", "cli-win32-arm64", "package.json");
  let inspected;
  const result = resolveBinary(target, {
    resolvePackageJson(request) {
      assert.equal(request, "@api429/cli-win32-arm64/package.json");
      return packageJson;
    },
    statSync(file) {
      inspected = file;
      return { isFile: () => true };
    },
  });

  assert.equal(result, path.join(path.dirname(packageJson), "bin", "api429.exe"));
  assert.equal(inspected, result);
});

test("resolveBinary reports an omitted optional package and a missing payload", () => {
  const target = selectTarget({ platform: "darwin", arch: "x64" });
  assert.throws(
    () =>
      resolveBinary(target, {
        resolvePackageJson() {
          const error = new Error("not found");
          error.code = "MODULE_NOT_FOUND";
          throw error;
        },
      }),
    /required optional package @api429\/cli-darwin-x64 is not installed/,
  );

  assert.throws(
    () =>
      resolveBinary(target, {
        resolvePackageJson: () => path.join("pkg", "package.json"),
        statSync() {
          const error = new Error("missing");
          error.code = "ENOENT";
          throw error;
        },
      }),
    /native executable is missing/,
  );
});

test("launch passes argv and inherited stdio and returns the child exit code", () => {
  const child = fakeChild();
  const processLike = fakeProcess();
  let invocation;
  const result = launch({
    argv: ["image", "generate", "--prompt", "two words"],
    processLike,
    resolvePackageJson: () => path.join("pkg", "package.json"),
    spawnImpl(file, args, options) {
      invocation = { args, file, options };
      return child;
    },
    statSync: () => ({ isFile: () => true }),
  });

  assert.equal(result, child);
  assert.deepEqual(invocation, {
    args: ["image", "generate", "--prompt", "two words"],
    file: path.join("pkg", "bin", "api429"),
    options: { shell: false, stdio: "inherit", windowsHide: false },
  });

  child.emit("exit", 23, null);
  assert.equal(processLike.exitCode, 23);
});

test("launch forwards termination signals to the child and mirrors child signals", () => {
  const child = fakeChild();
  const forwarded = [];
  child.kill = (signal) => forwarded.push(signal);
  const mirrored = [];
  const processLike = fakeProcess({
    kill(pid, signal) {
      mirrored.push([pid, signal]);
    },
  });

  launch({
    processLike,
    resolvePackageJson: () => path.join("pkg", "package.json"),
    spawnImpl: () => child,
    statSync: () => ({ isFile: () => true }),
  });

  processLike.emit("SIGINT");
  assert.deepEqual(forwarded, ["SIGINT"]);

  child.emit("exit", null, "SIGTERM");
  assert.deepEqual(mirrored, [[4242, "SIGTERM"]]);
  assert.equal(processLike.listenerCount("SIGINT"), 0);
});

test("launch uses a conventional signal exit code when re-signalling fails", () => {
  const child = fakeChild();
  const processLike = fakeProcess({
    kill() {
      throw new Error("not supported");
    },
  });

  launch({
    processLike,
    resolvePackageJson: () => path.join("pkg", "package.json"),
    spawnImpl: () => child,
    statSync: () => ({ isFile: () => true }),
  });
  child.emit("exit", null, "SIGTERM");

  assert.equal(processLike.exitCode, signalExitCode("SIGTERM"));
});

test("launch turns resolution and spawn failures into concise diagnostics", () => {
  const messages = [];
  const processLike = fakeProcess({ stderr: { write: (value) => messages.push(value) } });
  let spawned = false;
  assert.equal(
    launch({
      processLike,
      resolvePackageJson() {
        throw new Error("not found");
      },
      spawnImpl() {
        spawned = true;
      },
    }),
    null,
  );
  assert.equal(spawned, false);
  assert.equal(processLike.exitCode, 1);
  assert.match(messages.join(""), /required optional package/);

  messages.length = 0;
  processLike.exitCode = undefined;
  const child = fakeChild();
  launch({
    processLike,
    resolvePackageJson: () => path.join("pkg", "package.json"),
    spawnImpl: () => child,
    statSync: () => ({ isFile: () => true }),
  });
  child.emit("error", Object.assign(new Error("permission denied"), { code: "EACCES" }));
  assert.equal(processLike.exitCode, 1);
  assert.match(messages.join(""), /failed to start .*permission denied/);
});

test(
  "launcher preserves argv, stdin, stdout, stderr, and exit status with a real child",
  { skip: process.platform === "win32" },
  () => {
    const temporaryRoot = fs.mkdtempSync(path.join(os.tmpdir(), "api429-launcher-"));
    try {
      const packageDirectory = path.join(temporaryRoot, "platform-package");
      const binDirectory = path.join(packageDirectory, "bin");
      fs.mkdirSync(binDirectory, { recursive: true });
      const packageJson = path.join(packageDirectory, "package.json");
      fs.writeFileSync(packageJson, "{}\n");

      const executable = path.join(binDirectory, "api429");
      fs.writeFileSync(
        executable,
        [
          "#!/usr/bin/env node",
          '"use strict";',
          'let input = "";',
          'process.stdin.setEncoding("utf8");',
          'process.stdin.on("data", (chunk) => { input += chunk; });',
          'process.stdin.on("end", () => {',
          '  process.stdout.write(JSON.stringify({ argv: process.argv.slice(2), input }) + "\\n");',
          '  process.stderr.write("payload-stderr\\n");',
          "  process.exitCode = 37;",
          "});",
          "",
        ].join("\n"),
      );
      fs.chmodSync(executable, 0o755);

      const helper = path.join(temporaryRoot, "helper.js");
      const launcher = path.resolve(__dirname, "..", "lib", "launcher.js");
      fs.writeFileSync(
        helper,
        [
          `const { launch } = require(${JSON.stringify(launcher)});`,
          `launch({ resolvePackageJson: () => ${JSON.stringify(packageJson)} });`,
          "",
        ].join("\n"),
      );

      const result = spawnSync(
        process.execPath,
        [helper, "alpha", "two words", "--flag=value"],
        { encoding: "utf8", input: "payload-stdin" },
      );
      assert.equal(result.status, 37, result.stderr);
      assert.deepEqual(JSON.parse(result.stdout), {
        argv: ["alpha", "two words", "--flag=value"],
        input: "payload-stdin",
      });
      assert.equal(result.stderr, "payload-stderr\n");
    } finally {
      fs.rmSync(temporaryRoot, { force: true, recursive: true });
    }
  },
);
