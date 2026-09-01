"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const {
  packageDirectory,
  platforms,
  validate,
  workspaceRoot,
} = require("./validate-packages.js");

function isInside(parent, candidate) {
  const relative = path.relative(parent, candidate);
  return relative === "" || (!relative.startsWith(`..${path.sep}`) && relative !== "..");
}

function expectedTarballFilename(name, version) {
  return `${name.replace(/^@/, "").replaceAll("/", "-")}-${version}.tgz`;
}

function sha512(file) {
  return crypto.createHash("sha512").update(fs.readFileSync(file)).digest("hex");
}

function packPackage(packageName, outputDirectory, spawnSyncImpl = spawnSync) {
  const directory = packageDirectory(packageName);
  const manifest = JSON.parse(fs.readFileSync(path.join(directory, "package.json"), "utf8"));
  const expectedTarball = path.join(
    outputDirectory,
    expectedTarballFilename(manifest.name, manifest.version),
  );
  if (fs.existsSync(expectedTarball)) {
    throw new Error(`refusing to overwrite existing release artifact: ${expectedTarball}`);
  }

  const npm = process.platform === "win32" ? "npm.cmd" : "npm";
  const result = spawnSyncImpl(
    npm,
    ["pack", "--json", "--ignore-scripts", "--pack-destination", outputDirectory],
    { cwd: directory, encoding: "utf8" },
  );
  if (result.status !== 0) {
    throw new Error(
      `npm pack failed for ${packageName}\n${result.stderr || result.stdout}`,
    );
  }

  let packed;
  try {
    const parsed = JSON.parse(result.stdout);
    if (!Array.isArray(parsed) || parsed.length !== 1) throw new Error("unexpected result count");
    [packed] = parsed;
  } catch (error) {
    throw new Error(`could not parse npm pack JSON for ${packageName}: ${result.stdout}`, {
      cause: error,
    });
  }

  const tarball = path.resolve(outputDirectory, packed.filename);
  if (!fs.existsSync(tarball)) {
    throw new Error(`npm reported a tarball that does not exist: ${tarball}`);
  }
  const packedFiles = new Set(packed.files.map(({ path: file }) => file));
  const platform = platforms.find(({ name }) => name === packageName);
  const requiredFiles = platform
    ? ["README.md", "package.json", `bin/${platform.binary}`]
    : ["README.md", "package.json", "bin/api429.js", "lib/launcher.js"];
  for (const required of requiredFiles) {
    if (!packedFiles.has(required)) {
      throw new Error(`${packageName} tarball would omit required file: ${required}`);
    }
  }
  if (packedFiles.size !== requiredFiles.length) {
    const extras = [...packedFiles].filter((file) => !requiredFiles.includes(file));
    throw new Error(`${packageName} tarball contains unexpected files: ${extras.join(", ")}`);
  }
  return {
    name: packed.name,
    version: packed.version,
    tarball,
    sha512: sha512(tarball),
  };
}

function main(argv = process.argv.slice(2), environment = process.env) {
  if (argv.length > 1) {
    throw new Error("usage: node scripts/pack-release.js [OUTPUT_DIR]");
  }
  const requestedOutput = argv[0] || environment.API429_NPM_PACK_DIR;
  if (!requestedOutput) {
    throw new Error(
      "release output directory is required as argv[0] or API429_NPM_PACK_DIR",
    );
  }

  const outputDirectory = path.resolve(requestedOutput);
  for (const packageName of ["@api429/cli", ...platforms.map(({ name }) => name)]) {
    const directory = packageDirectory(packageName);
    if (isInside(directory, outputDirectory)) {
      throw new Error(`release output directory must be outside package directories: ${directory}`);
    }
  }

  validate({ quiet: true, requirePayload: true });
  fs.mkdirSync(outputDirectory, { recursive: true });

  const orderedPackageNames = [
    ...platforms.map(({ name }) => name),
    "@api429/cli",
  ];
  const stagingDirectory = fs.mkdtempSync(
    path.join(outputDirectory, ".api429-npm-pack-"),
  );
  const movedTarballs = [];
  let packages;
  try {
    const stagedPackages = orderedPackageNames.map((packageName) =>
      packPackage(packageName, stagingDirectory),
    );
    packages = stagedPackages.map((entry) => {
      const tarball = path.join(outputDirectory, path.basename(entry.tarball));
      if (fs.existsSync(tarball)) {
        throw new Error(`refusing to overwrite existing release artifact: ${tarball}`);
      }
      fs.renameSync(entry.tarball, tarball);
      movedTarballs.push(tarball);
      return { ...entry, tarball };
    });
  } catch (error) {
    for (const tarball of movedTarballs) {
      try {
        fs.unlinkSync(tarball);
      } catch {
        // Preserve the original packaging error; these paths were created by this run.
      }
    }
    throw error;
  } finally {
    fs.rmSync(stagingDirectory, { force: true, recursive: true });
  }
  const version = packages.at(-1).version;
  process.stdout.write(
    `${JSON.stringify({ schemaVersion: 1, version, packages }, null, 2)}\n`,
  );
  return { packages, version };
}

if (require.main === module) {
  try {
    main();
  } catch (error) {
    console.error(error instanceof Error ? error.message : error);
    process.exitCode = 1;
  }
}

module.exports = {
  expectedTarballFilename,
  isInside,
  main,
  packPackage,
  sha512,
  workspaceRoot,
};
