//! Cargo launcher and rustc wrapper for the pinned LLVM obfuscation toolchain.

use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::env;
use std::ffi::{OsStr, OsString};
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode, Stdio};
use std::time::{SystemTime, UNIX_EPOCH};

const PASSES: &[&str] = &[
    "obf-string",
    "obf-split",
    "obf-bcf",
    "obf-fla",
    "obf-sub",
    "obf-const",
    "obf-global-access",
];

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Config {
    version: u32,
    rustc: PathBuf,
    #[serde(default)]
    cargo: Option<PathBuf>,
    #[serde(default)]
    seed: Option<String>,
    #[serde(default)]
    strict: bool,
    packages: Vec<Rule>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Rule {
    name: String,
    #[serde(default)]
    version: Option<String>,
    source: String,
    #[serde(default)]
    targets: Vec<String>,
    #[serde(default)]
    crate_types: Vec<String>,
    passes: Vec<String>,
    #[serde(default)]
    functions: Vec<String>,
    #[serde(default)]
    globals: Vec<String>,
    #[serde(default)]
    constants: Vec<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct ResolvedRule {
    rule: Rule,
    package_id: String,
    manifest_dir: PathBuf,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct Runtime {
    config: Config,
    rules: Vec<ResolvedRule>,
    host: String,
    work_dir: PathBuf,
    target_dir: PathBuf,
    reused_target_dir: bool,
}

#[derive(Debug, Deserialize)]
struct Metadata {
    packages: Vec<MetadataPackage>,
    workspace_members: Vec<String>,
    workspace_root: PathBuf,
}

#[derive(Debug, Deserialize)]
struct MetadataPackage {
    id: String,
    name: String,
    version: String,
    source: Option<String>,
    manifest_path: PathBuf,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct Invocation {
    package_id: Option<String>,
    rule_index: Option<usize>,
    crate_name: Option<String>,
    crate_type: Vec<String>,
    target: Option<String>,
    status: String,
    exit_code: i32,
    linked_artifact: bool,
    event_file: Option<PathBuf>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct Event {
    event: String,
    pass: String,
    kind: String,
    raw_name: String,
    demangled_name: String,
    count: u64,
    #[serde(default)]
    reason: Option<String>,
}

#[derive(Serialize)]
struct PassSummary {
    matched_symbols: usize,
    transformed_symbols: usize,
    transformed_sites: u64,
    skipped_symbols: usize,
    unmatched_functions: Vec<String>,
    unmatched_globals: Vec<String>,
}

#[derive(Serialize)]
struct RuleReport {
    package_id: String,
    rule: Rule,
    compiled: usize,
    pass_summary: BTreeMap<String, PassSummary>,
    events: Vec<Event>,
}

#[derive(Serialize)]
struct BuildReport {
    schema_version: u32,
    rustc: PathBuf,
    cargo_command: Vec<String>,
    cargo_exit_code: i32,
    target_dir: PathBuf,
    reused_target_dir: bool,
    code_artifact: bool,
    coverage_status: String,
    strict_passed: bool,
    invocations: Vec<Invocation>,
    packages: Vec<RuleReport>,
}

fn main() -> ExitCode {
    let result = if env::var_os("RUST_OBF_WRAPPER_MODE").is_some() {
        wrapper()
    } else {
        launcher()
    };
    match result {
        Ok(code) => ExitCode::from((code.clamp(0, 255)) as u8),
        Err(error) => {
            eprintln!("rust-obf-cargo: {error}");
            ExitCode::FAILURE
        }
    }
}

fn fail(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message.into())
}

fn read_json<T: for<'de> Deserialize<'de>>(path: &Path) -> io::Result<T> {
    let data = fs::read(path)?;
    serde_json::from_slice(&data).map_err(|error| fail(format!("{}: {error}", path.display())))
}

fn write_json<T: Serialize>(path: &Path, value: &T) -> io::Result<()> {
    let data = serde_json::to_vec_pretty(value).map_err(io::Error::other)?;
    let mut data = data;
    data.push(b'\n');
    fs::write(path, data)
}

fn valid_atom(value: &str) -> bool {
    !value.is_empty() && !value.chars().any(|ch| ch.is_whitespace() || ch == ',')
}

fn validate(config: &Config) -> io::Result<()> {
    if config.version != 1 || config.packages.is_empty() {
        return Err(fail("config needs version=1 and at least one package rule"));
    }
    if let Some(seed) = &config.seed {
        if seed.len() != 32 || !seed.bytes().all(|ch| ch.is_ascii_hexdigit()) {
            return Err(fail("seed must be exactly 32 hexadecimal characters"));
        }
    }
    for rule in &config.packages {
        if !matches!(rule.source.as_str(), "workspace" | "registry") {
            return Err(fail(format!(
                "{}: source must be workspace or registry",
                rule.name
            )));
        }
        if rule.passes.is_empty() || rule.passes.iter().any(|p| !PASSES.contains(&p.as_str())) {
            return Err(fail(format!("{}: unknown or empty pass list", rule.name)));
        }
        if rule.passes.iter().collect::<BTreeSet<_>>().len() != rule.passes.len() {
            return Err(fail(format!("{}: repeated pass", rule.name)));
        }
        for item in rule
            .functions
            .iter()
            .chain(rule.globals.iter())
            .chain(rule.constants.iter())
        {
            if !valid_atom(item) {
                return Err(fail(format!(
                    "{}: selections cannot be empty or contain spaces/commas",
                    rule.name
                )));
            }
        }
        if rule.passes.iter().any(|p| p == "obf-global-access") && rule.globals.is_empty() {
            return Err(fail(format!(
                "{}: obf-global-access needs exact global names",
                rule.name
            )));
        }
        if rule.passes.iter().any(|p| p == "obf-const") && rule.constants.is_empty() {
            return Err(fail(format!(
                "{}: obf-const needs typed constants",
                rule.name
            )));
        }
        if rule.crate_types.iter().any(|kind| {
            !matches!(
                kind.as_str(),
                "bin" | "rlib" | "dylib" | "cdylib" | "staticlib"
            )
        }) {
            return Err(fail(format!("{}: unsupported crate type", rule.name)));
        }
    }
    Ok(())
}

fn cargo_metadata(cargo: &Path, cargo_args: &[OsString]) -> io::Result<Metadata> {
    let mut command = Command::new(cargo);
    command.args(["metadata", "--format-version", "1"]);
    let mut i = 0;
    while i < cargo_args.len() {
        let arg = cargo_args[i].to_string_lossy();
        if matches!(arg.as_ref(), "--manifest-path" | "--features" | "--config") {
            if let Some(value) = cargo_args.get(i + 1) {
                command.arg(&cargo_args[i]).arg(value);
                i += 2;
                continue;
            }
        }
        if matches!(
            arg.as_ref(),
            "--locked" | "--offline" | "--frozen" | "--all-features" | "--no-default-features"
        ) || arg.starts_with("--manifest-path=")
            || arg.starts_with("--features=")
        {
            command.arg(&cargo_args[i]);
        }
        i += 1;
    }
    let output = command.output()?;
    if !output.status.success() {
        return Err(fail(format!(
            "cargo metadata failed:\n{}",
            String::from_utf8_lossy(&output.stderr)
        )));
    }
    serde_json::from_slice(&output.stdout).map_err(io::Error::other)
}

fn resolve_rules(config: &Config, metadata: &Metadata) -> io::Result<Vec<ResolvedRule>> {
    let members: BTreeSet<_> = metadata.workspace_members.iter().collect();
    config.packages.iter().map(|rule| {
        let candidates: Vec<_> = metadata.packages.iter().filter(|package| {
            let source_match = if rule.source == "workspace" {
                members.contains(&package.id)
            } else {
                package.source.as_deref().is_some_and(|source| source.starts_with("registry+"))
            };
            source_match && package.name == rule.name
                && rule.version.as_deref().is_none_or(|version| version == package.version)
        }).collect();
        if candidates.len() != 1 {
            return Err(fail(format!(
                "{}: expected one {} package in cargo metadata, found {}; add a version if ambiguous",
                rule.name, rule.source, candidates.len()
            )));
        }
        let package = candidates[0];
        let manifest_dir = package.manifest_path.parent()
            .ok_or_else(|| fail("package manifest has no parent"))?.canonicalize()?;
        Ok(ResolvedRule {
            rule: rule.clone(),
            package_id: package.id.clone(),
            manifest_dir,
        })
    }).collect()
}

fn rust_host(rustc: &Path) -> io::Result<String> {
    let output = Command::new(rustc).arg("-vV").output()?;
    if !output.status.success() {
        return Err(fail(format!("{} -vV failed", rustc.display())));
    }
    let text = String::from_utf8_lossy(&output.stdout);
    if !text.lines().any(|line| line.starts_with("release: 1.99."))
        || !text
            .lines()
            .any(|line| line.starts_with("LLVM version: 23."))
    {
        return Err(fail(
            "wrapper requires the pinned Rust 1.99 / LLVM 23 compiler",
        ));
    }
    text.lines()
        .find_map(|line| line.strip_prefix("host: ").map(str::to_owned))
        .ok_or_else(|| fail("rustc -vV has no host triple"))
}

fn unique_dir(base: &Path) -> io::Result<PathBuf> {
    fs::create_dir_all(base)?;
    for retry in 0..16u32 {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(io::Error::other)?
            .as_nanos();
        let path = base.join(format!("run-{}-{nanos}-{retry}", std::process::id()));
        match fs::create_dir(&path) {
            Ok(()) => return Ok(path),
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(error),
        }
    }
    Err(fail("could not allocate a unique report directory"))
}

fn launcher() -> io::Result<i32> {
    let args: Vec<OsString> = env::args_os().skip(1).collect();
    let mut config_path = None;
    let mut report_path = None;
    let mut reuse_target_dir = None;
    let mut separator = None;
    let mut i = 0;
    while i < args.len() {
        let arg = args[i].to_string_lossy();
        if arg == "--" {
            separator = Some(i);
            break;
        }
        if matches!(arg.as_ref(), "--config" | "--report" | "--reuse-target-dir") {
            let value = args
                .get(i + 1)
                .ok_or_else(|| fail(format!("{arg} needs a path")))?;
            match arg.as_ref() {
                "--config" => config_path = Some(PathBuf::from(value)),
                "--report" => report_path = Some(PathBuf::from(value)),
                "--reuse-target-dir" => reuse_target_dir = Some(PathBuf::from(value)),
                _ => unreachable!(),
            }
            i += 2;
            continue;
        }
        return Err(fail(
            "usage: rust-obf-cargo --config FILE --report FILE [--reuse-target-dir DIR] -- build [Cargo args]",
        ));
    }
    let separator = separator.ok_or_else(|| fail("missing -- before Cargo command"))?;
    let cargo_args = &args[separator + 1..];
    if cargo_args.is_empty() {
        return Err(fail("missing Cargo command"));
    }
    let cargo_subcommand = cargo_args[0].to_string_lossy().to_string();
    if !matches!(
        cargo_subcommand.as_str(),
        "build" | "check" | "test" | "run" | "rustc"
    ) {
        return Err(fail(
            "supported Cargo commands: build, check, test, run, rustc",
        ));
    }
    if env::var_os("RUSTC_WRAPPER").is_some() || env::var_os("RUSTC_WORKSPACE_WRAPPER").is_some() {
        return Err(fail(
            "unset existing RUSTC_WRAPPER and RUSTC_WORKSPACE_WRAPPER for this build",
        ));
    }
    if cargo_args.iter().any(|arg| {
        arg == OsStr::new("--target-dir") || arg.to_string_lossy().starts_with("--target-dir=")
    }) {
        return Err(fail(
            "the wrapper owns Cargo's target directory for an auditable fresh build",
        ));
    }
    let config_path = config_path
        .ok_or_else(|| fail("missing --config"))?
        .canonicalize()?;
    let report_path = report_path.ok_or_else(|| fail("missing --report"))?;
    let report_path = if report_path.is_absolute() {
        report_path
    } else {
        env::current_dir()?.join(report_path)
    };
    fs::create_dir_all(
        report_path
            .parent()
            .ok_or_else(|| fail("report has no parent"))?,
    )?;
    let config: Config = read_json(&config_path)?;
    validate(&config)?;
    let rustc = config.rustc.canonicalize()?;
    let host = rust_host(&rustc)?;
    let cargo = config
        .cargo
        .clone()
        .unwrap_or_else(|| PathBuf::from("cargo"));
    let metadata = cargo_metadata(&cargo, cargo_args)?;
    let lockfile = metadata.workspace_root.join("Cargo.lock");
    let rules = resolve_rules(&config, &metadata)?;
    let work_dir = unique_dir(&report_path.with_extension("rust-obf-events"))?;
    fs::create_dir(work_dir.join("invocations"))?;
    fs::create_dir(work_dir.join("events"))?;
    let reused_target_dir = reuse_target_dir.is_some();
    let target_dir = if let Some(path) = reuse_target_dir {
        let absolute = if path.is_absolute() {
            path
        } else {
            env::current_dir()?.join(path)
        };
        fs::create_dir_all(&absolute)?;
        absolute.canonicalize()?
    } else {
        work_dir.join("target")
    };
    let runtime = Runtime {
        config: Config {
            rustc: rustc.clone(),
            ..config
        },
        rules,
        host: host.clone(),
        work_dir: work_dir.clone(),
        target_dir: target_dir.clone(),
        reused_target_dir,
    };
    let wrapper = env::current_exe()?.canonicalize()?;
    if reused_target_dir {
        // Cargo does not see the rustc flags added inside this wrapper when
        // it fingerprints dependencies. Refuse to reuse artifacts built with
        // a different selection, seed, wrapper, compiler, build profile, or
        // dependency lockfile. A no-op reuse still yields no fresh coverage.
        let stamp = |path: &Path| -> io::Result<(u64, u128)> {
            let metadata = fs::metadata(path)?;
            let modified = metadata
                .modified()?
                .duration_since(UNIX_EPOCH)
                .map_err(io::Error::other)?
                .as_nanos();
            Ok((metadata.len(), modified))
        };
        let lock_data = match fs::read(&lockfile) {
            Ok(data) => Some(data),
            Err(error) if error.kind() == io::ErrorKind::NotFound => None,
            Err(error) => return Err(error),
        };
        let identity = serde_json::to_vec(&(
            &runtime.config,
            &runtime.rules,
            cargo_args
                .iter()
                .map(|arg| arg.to_string_lossy().to_string())
                .collect::<Vec<_>>(),
            stamp(&rustc)?,
            stamp(&wrapper)?,
            lock_data,
        ))
        .map_err(io::Error::other)?;
        let guard = target_dir.join(".rust-obf-target-identity");
        if guard.exists() {
            if fs::read(&guard)? != identity {
                return Err(fail(format!(
                    "{}: target was built with a different build identity; use a fresh directory",
                    target_dir.display()
                )));
            }
        } else {
            if fs::read_dir(&target_dir)?.next().is_some() {
                return Err(fail(format!(
                    "{}: target is not empty and has no obfuscation identity",
                    target_dir.display()
                )));
            }
            fs::write(guard, identity)?;
        }
    }
    let runtime_path = work_dir.join("runtime.json");
    write_json(&runtime_path, &runtime)?;
    let mut command = Command::new(&cargo);
    command.args(cargo_args);
    if !cargo_args
        .iter()
        .any(|arg| arg == OsStr::new("--target") || arg.to_string_lossy().starts_with("--target="))
    {
        command.arg("--target").arg(&host);
    }
    command
        .env("RUSTC", &rustc)
        .env("RUSTC_WRAPPER", &wrapper)
        .env("RUST_OBF_WRAPPER_MODE", "1")
        .env("RUST_OBF_RUNTIME_CONFIG", &runtime_path)
        .env("CARGO_TARGET_DIR", &target_dir)
        .stdin(Stdio::inherit())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit());
    let status = command.status()?;
    let cargo_exit_code = status.code().unwrap_or(1);
    let check = cargo_subcommand == "check";
    let report = aggregate(&runtime, cargo_args, cargo_exit_code, check)?;
    write_json(&report_path, &report)?;
    eprintln!("rust-obf-cargo: report {}", report_path.display());
    if check {
        eprintln!("rust-obf-cargo: cargo check produced no protected code artifact");
    }
    if cargo_exit_code != 0 {
        return Ok(cargo_exit_code);
    }
    if runtime.config.strict && !report.strict_passed {
        return Ok(2);
    }
    Ok(0)
}

fn option_values(args: &[OsString], option: &str) -> Vec<String> {
    let mut values = Vec::new();
    let mut i = 0;
    while i < args.len() {
        let value = args[i].to_string_lossy();
        if value == option {
            if let Some(next) = args.get(i + 1) {
                values.push(next.to_string_lossy().to_string());
            }
            i += 2;
            continue;
        }
        if let Some(rest) = value.strip_prefix(&format!("{option}=")) {
            values.push(rest.to_owned());
        }
        i += 1;
    }
    values
}

fn crate_types(args: &[OsString]) -> Vec<String> {
    option_values(args, "--crate-type")
        .into_iter()
        .flat_map(|value| {
            value
                .split(',')
                .map(|kind| {
                    if kind == "lib" {
                        "rlib".to_owned()
                    } else {
                        kind.to_owned()
                    }
                })
                .collect::<Vec<_>>()
        })
        .collect()
}

fn selected_rule<'a>(
    runtime: &'a Runtime,
    args: &[OsString],
) -> io::Result<Option<(usize, &'a ResolvedRule)>> {
    let manifest_dir = match env::var_os("CARGO_MANIFEST_DIR") {
        Some(path) => PathBuf::from(path).canonicalize()?,
        None => return Ok(None),
    };
    let crate_name = option_values(args, "--crate-name").into_iter().next();
    let types = crate_types(args);
    let target = option_values(args, "--target").into_iter().next();
    if target.is_none()
        || types.iter().any(|kind| kind == "proc-macro")
        || crate_name.as_deref() == Some("build_script_build")
    {
        return Ok(None);
    }
    for (index, resolved) in runtime.rules.iter().enumerate() {
        let rule = &resolved.rule;
        if resolved.manifest_dir != manifest_dir {
            continue;
        }
        if !rule.targets.is_empty()
            && !crate_name.as_ref().is_some_and(|name| {
                rule.targets
                    .iter()
                    .any(|target| target.replace('-', "_") == *name)
            })
        {
            continue;
        }
        if !rule.crate_types.is_empty() && !types.iter().any(|kind| rule.crate_types.contains(kind))
        {
            continue;
        }
        return Ok(Some((index, resolved)));
    }
    Ok(None)
}

fn wrapper() -> io::Result<i32> {
    let runtime_path = PathBuf::from(
        env::var_os("RUST_OBF_RUNTIME_CONFIG").ok_or_else(|| fail("missing runtime config"))?,
    );
    let runtime: Runtime = read_json(&runtime_path)?;
    let mut args: Vec<OsString> = env::args_os().skip(1).collect();
    if args.is_empty() {
        return Err(fail("Cargo supplied no rustc executable"));
    }
    let rustc = args.remove(0);
    let crate_name = option_values(&args, "--crate-name").into_iter().next();
    let types = crate_types(&args);
    let target = option_values(&args, "--target").into_iter().next();
    let selection = selected_rule(&runtime, &args)?;
    let emit_options = option_values(&args, "--emit");
    let metadata_only = !emit_options.is_empty()
        && emit_options.iter().all(|value| {
            !value
                .split(',')
                .any(|emit| matches!(emit, "link" | "llvm-ir" | "obj" | "asm" | "llvm-bc"))
        });
    let linked_artifact = emit_options.is_empty()
        || emit_options
            .iter()
            .any(|value| value.split(',').any(|emit| matches!(emit, "link" | "obj")));
    let selected = selection.as_ref().filter(|_| !metadata_only);
    let suffix = format!(
        "{}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(io::Error::other)?
            .as_nanos()
    );
    let event_file = selected.map(|_| {
        runtime
            .work_dir
            .join("events")
            .join(format!("{suffix}.jsonl"))
    });
    if let Some((_, resolved)) = selected {
        let rule = &resolved.rule;
        // Apply a selected package's late passes before LTO imports/merges
        // modules. The final app invocation is often unselected, and its
        // post-link pipeline has no reliable package identity for imports.
        let mut llvm = format!(
            "-rust-obf-pipeline={} -rust-obf-prelink-only",
            rule.passes.join(",")
        );
        if let Some(seed) = &runtime.config.seed {
            llvm.push_str(&format!(" -obf-test-seed={seed}"));
        }
        llvm.push_str(" -obf-report-skips");
        if !rule.functions.is_empty() {
            llvm.push_str(&format!(
                " -obf-only-functions={}",
                rule.functions.join(",")
            ));
        }
        if !rule.globals.is_empty() {
            if rule.passes.iter().any(|pass| pass == "obf-string") {
                llvm.push_str(&format!(" -sobf-only-globals={}", rule.globals.join(",")));
            }
            if rule.passes.iter().any(|pass| pass == "obf-global-access") {
                llvm.push_str(&format!(" -gai-only-globals={}", rule.globals.join(",")));
            }
        }
        if !rule.constants.is_empty() {
            llvm.push_str(&format!(" -constenc-values={}", rule.constants.join(",")));
        }
        args.push(OsString::from("-C"));
        args.push(OsString::from(format!("llvm-args={llvm}")));
    }
    let mut command = Command::new(rustc);
    command
        .args(&args)
        .stdin(Stdio::inherit())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit());
    if let Some(path) = &event_file {
        command.env("RUST_OBF_EVENT_FILE", path);
    }
    let status = command.status()?;
    let code = status.code().unwrap_or(1);
    if crate_name.is_some() {
        let invocation = Invocation {
            package_id: selection.as_ref().map(|(_, rule)| rule.package_id.clone()),
            rule_index: selection.as_ref().map(|(index, _)| *index),
            crate_name,
            crate_type: types,
            target,
            status: if selected.is_some() {
                "selected".to_owned()
            } else if metadata_only && selection.is_some() {
                "metadata-only".to_owned()
            } else {
                "unselected".to_owned()
            },
            exit_code: code,
            linked_artifact,
            event_file,
        };
        write_json(
            &runtime
                .work_dir
                .join("invocations")
                .join(format!("{suffix}.json")),
            &invocation,
        )?;
    }
    Ok(code)
}

fn aggregate(
    runtime: &Runtime,
    cargo_args: &[OsString],
    cargo_exit_code: i32,
    check: bool,
) -> io::Result<BuildReport> {
    let mut invocations = Vec::new();
    for entry in fs::read_dir(runtime.work_dir.join("invocations"))? {
        invocations.push(read_json::<Invocation>(&entry?.path())?);
    }
    invocations.sort_by(|a, b| {
        a.package_id
            .cmp(&b.package_id)
            .then(a.crate_name.cmp(&b.crate_name))
    });
    let mut reports = Vec::new();
    let mut strict_passed = true;
    for (index, resolved) in runtime.rules.iter().enumerate() {
        let selected: Vec<_> = invocations
            .iter()
            .filter(|item| item.rule_index == Some(index))
            .collect();
        let mut events = Vec::new();
        for item in &selected {
            if let Some(path) = &item.event_file {
                if path.exists() {
                    for line in fs::read_to_string(path)?.lines() {
                        events.push(serde_json::from_str::<Event>(line).map_err(io::Error::other)?);
                    }
                }
            }
        }
        let mut pass_summary = BTreeMap::new();
        for pass in &resolved.rule.passes {
            let short_pass = match pass.as_str() {
                "obf-string" => "sobf",
                "obf-split" => "split",
                "obf-bcf" => "bcf",
                "obf-fla" => "fla",
                "obf-sub" => "sub",
                "obf-const" => "constenc",
                "obf-global-access" => "gai",
                _ => unreachable!(),
            };
            let global_pass = matches!(pass.as_str(), "obf-string" | "obf-global-access");
            let primary_kind = if global_pass { "global" } else { "function" };
            let related: Vec<_> = events
                .iter()
                .filter(|event| event.pass == short_pass && event.kind == primary_kind)
                .collect();
            let function_events: Vec<_> = events
                .iter()
                .filter(|event| event.pass == short_pass && event.kind == "function")
                .collect();
            let matched_functions: BTreeSet<_> = function_events
                .iter()
                .filter(|event| {
                    !matches!(event.reason.as_deref(), Some("not-selected" | "not-found"))
                })
                .map(|event| event.raw_name.as_str())
                .collect();
            let transformed_functions: BTreeSet<_> = function_events
                .iter()
                .filter(|event| event.event == "effect")
                .map(|event| event.raw_name.as_str())
                .collect();
            let matched: BTreeSet<_> = related
                .iter()
                .filter(|event| {
                    !matches!(event.reason.as_deref(), Some("not-selected" | "not-found"))
                })
                .map(|event| event.raw_name.as_str())
                .collect();
            let transformed: BTreeSet<_> = related
                .iter()
                .filter(|event| event.event == "effect")
                .map(|event| event.raw_name.as_str())
                .collect();
            let skipped: BTreeSet<_> = events
                .iter()
                .filter(|event| {
                    event.pass == short_pass
                        && event.event == "skip"
                        && event.reason.as_deref() != Some("not-selected")
                })
                .map(|event| (event.kind.as_str(), event.raw_name.as_str()))
                .collect();
            let unmatched_functions: Vec<_> = resolved
                .rule
                .functions
                .iter()
                .filter(|name| !matched_functions.contains(name.as_str()))
                .cloned()
                .collect();
            let unmatched_globals = if !global_pass {
                Vec::new()
            } else {
                resolved
                    .rule
                    .globals
                    .iter()
                    .filter(|name| !matched.contains(name.as_str()))
                    .cloned()
                    .collect()
            };
            if transformed.is_empty()
                || !unmatched_functions.is_empty()
                || !unmatched_globals.is_empty()
                || resolved
                    .rule
                    .functions
                    .iter()
                    .any(|name| !transformed_functions.contains(name.as_str()))
                || (global_pass
                    && !resolved.rule.globals.is_empty()
                    && resolved
                        .rule
                        .globals
                        .iter()
                        .any(|name| !transformed.contains(name.as_str())))
            {
                strict_passed = false;
            }
            pass_summary.insert(
                pass.clone(),
                PassSummary {
                    matched_symbols: matched.len(),
                    transformed_symbols: transformed.len(),
                    transformed_sites: related
                        .iter()
                        .filter(|event| event.event == "effect")
                        .map(|event| event.count)
                        .sum(),
                    skipped_symbols: skipped.len(),
                    unmatched_functions,
                    unmatched_globals,
                },
            );
        }
        if selected.is_empty() {
            strict_passed = false;
        }
        reports.push(RuleReport {
            package_id: resolved.package_id.clone(),
            rule: resolved.rule.clone(),
            compiled: selected.len(),
            pass_summary,
            events,
        });
    }
    let code_artifact = !check
        && invocations
            .iter()
            .any(|item| item.status == "selected" && item.exit_code == 0 && item.linked_artifact);
    if !code_artifact && !check {
        strict_passed = false;
    }
    Ok(BuildReport {
        schema_version: 1,
        rustc: runtime.config.rustc.clone(),
        cargo_command: cargo_args
            .iter()
            .map(|arg| arg.to_string_lossy().to_string())
            .collect(),
        cargo_exit_code,
        target_dir: runtime.target_dir.clone(),
        reused_target_dir: runtime.reused_target_dir,
        code_artifact,
        coverage_status: if check {
            "no-protected-code-artifact"
        } else if code_artifact {
            "compiled-selected-crates"
        } else {
            "no-selected-crate-compiled"
        }
        .to_owned(),
        strict_passed: !check && strict_passed,
        invocations,
        packages: reports,
    })
}
