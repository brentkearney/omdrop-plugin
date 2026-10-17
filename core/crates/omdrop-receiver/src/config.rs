//! `~/.config/airdrop/config.toml` and the settings derived from it.
//!
//! The config file is the source of truth, written by `omdrop setup` and the
//! configuration commands in `omdrop`; flags override individual keys, so the
//! file can be absent. Validation lives here, never in whatever writes the
//! file: a GUI is a convenience for writing a value, not what makes it safe.
//! Any bad value refuses the start (reload = restart). Every message is the
//! Python receiver's, because `omdrop` shows them to the user.

use std::os::unix::fs::MetadataExt;
use std::path::{Component, Path, PathBuf};

use omdrop_proto::archive::{is_unsafe_char, py_repr};

pub const CONFIG_KEYS: [&str; 5] = [
    "name",
    "model",
    "download_dir",
    "max_receive_percent",
    "compact_tls",
];
const STRING_CONFIG_KEYS: [&str; 3] = ["name", "model", "download_dir"];
pub const DEFAULT_MAX_RECEIVE_PERCENT: i64 = 30;
pub const DEFAULT_MODEL: &str = "MacBookPro18,3";

/// What config.toml says, validated.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct FileConfig {
    pub compact_tls: Option<bool>,
    pub name: Option<String>,
    pub model: Option<String>,
    pub download_dir: Option<String>,
    pub max_receive_percent: Option<i64>,
}

impl FileConfig {
    /// The `config PATH: ...` log line's tail: sorted `key=repr` pairs, or
    /// `absent (defaults)` for an absent or empty file.
    pub fn summary(&self) -> String {
        let mut parts = Vec::new();
        if let Some(compact) = self.compact_tls {
            parts.push(format!("compact_tls={compact}"));
        }
        if let Some(d) = &self.download_dir {
            parts.push(format!("download_dir={}", py_repr(d)));
        }
        if let Some(p) = self.max_receive_percent {
            parts.push(format!("max_receive_percent={p}"));
        }
        if let Some(m) = &self.model {
            parts.push(format!("model={}", py_repr(m)));
        }
        if let Some(n) = &self.name {
            parts.push(format!("name={}", py_repr(n)));
        }
        if parts.is_empty() {
            "absent (defaults)".into()
        } else {
            parts.join(", ")
        }
    }
}

/// `$XDG_CONFIG_HOME/airdrop/config.toml`, else the passwd home's
/// `.config/airdrop/config.toml`.
pub fn default_config_path() -> PathBuf {
    let base = match std::env::var_os("XDG_CONFIG_HOME") {
        Some(dir) if !dir.is_empty() => PathBuf::from(dir),
        _ => passwd_home().join(".config"),
    };
    base.join("airdrop").join("config.toml")
}

pub fn passwd_home() -> PathBuf {
    omdrop_proto::contacts::passwd_home().unwrap_or_else(|| PathBuf::from("/"))
}

/// Read the TOML. Unknown keys are an error (a typo must not silently fall
/// back to a default); a missing file is fine.
pub fn load(path: &Path) -> Result<FileConfig, String> {
    let text = match std::fs::read(path) {
        Ok(bytes) => bytes,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(FileConfig::default()),
        Err(e) => return Err(format!("{}: {e}", path.display())),
    };
    let shown = path.display();
    let text =
        String::from_utf8(text).map_err(|_| format!("{shown}: the file is not valid UTF-8"))?;
    let doc: toml_edit::DocumentMut = text
        .parse()
        .map_err(|e: toml_edit::TomlError| format!("{shown}: {}", e.message()))?;
    let table = doc.as_table();
    let mut bad: Vec<&str> = table
        .iter()
        .map(|(k, _)| k)
        .filter(|k| !CONFIG_KEYS.contains(k))
        .collect();
    bad.sort_unstable();
    if !bad.is_empty() {
        return Err(format!(
            "{shown}: unknown key(s) {}; known: {}",
            bad.join(", "),
            CONFIG_KEYS.join(", ")
        ));
    }
    let mut cfg = FileConfig::default();
    for key in STRING_CONFIG_KEYS {
        let Some(value) = table.get(key) else {
            continue;
        };
        let value = value.as_str().filter(|s| !s.trim().is_empty());
        let Some(value) = value else {
            return Err(format!("{shown}: {key} must be a non-empty string"));
        };
        let slot = match key {
            "name" => &mut cfg.name,
            "model" => &mut cfg.model,
            _ => &mut cfg.download_dir,
        };
        *slot = Some(value.to_string());
    }
    if let Some(value) = table.get("max_receive_percent") {
        let Some(percent) = value.as_integer() else {
            return Err(percent_error());
        };
        cfg.max_receive_percent = Some(validate_receive_percent(percent)?);
    }
    if let Some(value) = table.get("compact_tls") {
        cfg.compact_tls = Some(value.as_bool().ok_or_else(|| {
            format!("{shown}: compact_tls must be a boolean")
        })?);
    }
    Ok(cfg)
}

fn percent_error() -> String {
    "max_receive_percent must be an integer from 1 to 90".into()
}

pub fn validate_receive_percent(value: i64) -> Result<i64, String> {
    if (1..=90).contains(&value) {
        Ok(value)
    } else {
        Err(percent_error())
    }
}

/// What the Mac sheet will draw: no path separators or control/format
/// characters, 1-63 characters.
pub fn validate_name(name: &str) -> Result<String, String> {
    let count = name.chars().count();
    if name.chars().any(is_unsafe_char) || count == 0 || count > 63 {
        return Err(format!(
            "name {}: 1-63 characters, no path separators or control/format characters",
            py_repr(name)
        ));
    }
    Ok(name.to_string())
}

/// `^[A-Za-z]+[0-9]+,[0-9]+$`: MacBookPro18,3, iMac21,1, Macmini9,1.
pub fn validate_model(model: &str) -> Result<String, String> {
    let letters = model.bytes().take_while(u8::is_ascii_alphabetic).count();
    let rest = model.get(letters..).unwrap_or_default();
    let ok = letters > 0
        && rest.split_once(',').is_some_and(|(major, minor)| {
            !major.is_empty()
                && !minor.is_empty()
                && major.bytes().all(|b| b.is_ascii_digit())
                && minor.bytes().all(|b| b.is_ascii_digit())
        });
    if !ok {
        return Err(format!(
            "model {}: must be an Apple model identifier such as MacBookPro18,3",
            py_repr(model)
        ));
    }
    Ok(model.to_string())
}

/// The short host name, as the user already knows this machine.
pub fn default_receiver_name() -> String {
    let mut buf = [0u8; 256];
    // SAFETY: `buf` is valid for `buf.len()` bytes; gethostname NUL-terminates
    // within it when the name fits.
    let rc = unsafe { libc::gethostname(buf.as_mut_ptr().cast(), buf.len()) };
    let end = buf.iter().position(|&b| b == 0).unwrap_or(buf.len());
    let host = if rc == 0 {
        String::from_utf8_lossy(buf.get(..end).unwrap_or_default()).into_owned()
    } else {
        String::new()
    };
    let short = host.split('.').next().unwrap_or_default();
    if short.is_empty() {
        "omdrop".into()
    } else {
        short.to_string()
    }
}

/// `os.path.expanduser`: `~` and `~/...` from `$HOME` (else passwd), `~user`
/// from the passwd database.
fn expand_user(path: &str) -> PathBuf {
    let Some(rest) = path.strip_prefix('~') else {
        return PathBuf::from(path);
    };
    let (user, tail) = match rest.find('/') {
        Some(i) => rest.split_at(i),
        None => (rest, ""),
    };
    let home = if user.is_empty() {
        Some(omdrop_proto::contacts::home_dir())
    } else {
        user_home(user)
    };
    match home {
        Some(home) => PathBuf::from(format!("{}{tail}", home.display())),
        None => PathBuf::from(path),
    }
}

fn user_home(user: &str) -> Option<PathBuf> {
    use std::ffi::{CStr, CString, OsStr};
    use std::os::unix::ffi::OsStrExt;
    let name = CString::new(user).ok()?;
    let mut buf = vec![0 as libc::c_char; 16 * 1024];
    let mut pwd = std::mem::MaybeUninit::<libc::passwd>::uninit();
    let mut result: *mut libc::passwd = std::ptr::null_mut();
    // SAFETY: all pointers are valid for the call; `buf` outlives the use of
    // the strings it holds.
    let rc = unsafe {
        libc::getpwnam_r(
            name.as_ptr(),
            pwd.as_mut_ptr(),
            buf.as_mut_ptr(),
            buf.len(),
            &mut result,
        )
    };
    if rc != 0 || result.is_null() {
        return None;
    }
    // SAFETY: getpwnam_r succeeded; pw_dir points into `buf`.
    let dir = unsafe { CStr::from_ptr((*result).pw_dir) };
    Some(PathBuf::from(OsStr::from_bytes(dir.to_bytes())))
}

/// `os.path.realpath` for a path that may not exist: canonical when it does,
/// else absolute with `.` and `..` folded.
fn real_path(path: &Path) -> PathBuf {
    if let Ok(real) = std::fs::canonicalize(path) {
        return real;
    }
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("/"))
            .join(path)
    };
    let mut out = PathBuf::from("/");
    for part in absolute.components() {
        match part {
            Component::ParentDir => {
                out.pop();
            }
            Component::Normal(p) => out.push(p),
            _ => {}
        }
    }
    out
}

/// Resolved once; must exist, be a directory, and be owned by this user.
/// Received files land here as this user, so a directory we do not own is a
/// misconfiguration, not something to create or chown our way out of.
pub fn validate_download_dir(path: &str) -> Result<PathBuf, String> {
    let dest = real_path(&expand_user(path));
    let meta = match std::fs::metadata(&dest) {
        Ok(m) => m,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return Err(format!(
                "download_dir {} -> {}: does not exist",
                py_repr(path),
                dest.display()
            ));
        }
        Err(e) => return Err(format!("download_dir {}: {e}", dest.display())),
    };
    if !meta.is_dir() {
        return Err(format!("download_dir {}: not a directory", dest.display()));
    }
    // SAFETY: getuid has no preconditions.
    let uid = unsafe { libc::getuid() };
    if meta.uid() != uid {
        return Err(format!(
            "download_dir {}: owned by uid {}, not us ({uid})",
            dest.display(),
            meta.uid()
        ));
    }
    Ok(dest)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn load_text(text: &str) -> Result<FileConfig, String> {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("config.toml");
        std::fs::write(&path, text).unwrap();
        load(&path).map_err(|e| e.replace(&format!("{}: ", path.display()), "PATH: "))
    }

    #[test]
    fn integer_receive_percentage_is_accepted() {
        let cfg = load_text("name = \"Drop\"\nmax_receive_percent = 45\n").unwrap();
        assert_eq!(cfg.max_receive_percent, Some(45));
        assert_eq!(cfg.summary(), "max_receive_percent=45, name='Drop'");
    }

    #[test]
    fn compact_tls_requires_a_boolean_and_defaults_to_disabled() {
        assert_eq!(load_text("").unwrap().compact_tls, None);
        for enabled in [false, true] {
            let cfg = load_text(&format!("compact_tls = {enabled}\n")).unwrap();
            assert_eq!(cfg.compact_tls, Some(enabled));
            assert_eq!(cfg.summary(), format!("compact_tls={enabled}"));
        }
        for value in ["1", "0", "\"true\"", "[]", "{}"] {
            assert_eq!(
                load_text(&format!("compact_tls = {value}\n")).unwrap_err(),
                "PATH: compact_tls must be a boolean"
            );
        }
    }

    #[test]
    fn bad_values_refuse_the_start_with_the_python_messages() {
        let percent = "max_receive_percent must be an integer from 1 to 90";
        for (text, want) in [
            ("max_receive_percent = \"45\"\n", percent),
            ("max_receive_percent = 45.0\n", percent),
            ("max_receive_percent = true\n", percent),
            ("max_receive_percent = 91\n", percent),
            ("max_receive_percent = 0\n", percent),
            (
                "nmae = \"x\"\nzz = 1\n",
                "PATH: unknown key(s) nmae, zz; known: name, model, download_dir, max_receive_percent, compact_tls",
            ),
            ("name = \"  \"\n", "PATH: name must be a non-empty string"),
            ("model = 3\n", "PATH: model must be a non-empty string"),
            (
                "[download_dir]\nx = 1\n",
                "PATH: download_dir must be a non-empty string",
            ),
        ] {
            assert_eq!(load_text(text).unwrap_err(), want, "{text}");
        }
        assert!(load_text("name = \n").unwrap_err().starts_with("PATH: "));
    }

    #[test]
    fn a_missing_or_empty_file_means_defaults() {
        assert_eq!(
            load(Path::new("/nonexistent/config.toml")).unwrap(),
            FileConfig::default()
        );
        assert_eq!(load_text("").unwrap().summary(), "absent (defaults)");
    }

    #[test]
    fn names_and_models_are_validated() {
        assert!(validate_name("Brent's MacBook").is_ok());
        assert!(validate_name(&"x".repeat(63)).is_ok());
        assert_eq!(
            validate_name("a/b").unwrap_err(),
            "name 'a/b': 1-63 characters, no path separators or control/format characters"
        );
        for bad in ["", "evil\u{202e}gpj", "tab\there", &"x".repeat(64)] {
            assert!(validate_name(bad).is_err(), "{bad:?}");
        }
        for good in ["MacBookPro18,3", "iMac21,1", "Macmini9,1"] {
            assert!(validate_model(good).is_ok());
        }
        for bad in [
            "",
            "18,3",
            "MacBookPro,3",
            "MacBookPro18,",
            "MacBookPro18,3\n",
            "Mac Book1,2",
        ] {
            assert!(validate_model(bad).is_err(), "{bad:?}");
        }
        assert_eq!(
            validate_model("x").unwrap_err(),
            "model 'x': must be an Apple model identifier such as MacBookPro18,3"
        );
    }

    #[test]
    fn download_dir_must_exist_be_a_directory_and_ours() {
        let dir = tempfile::tempdir().unwrap();
        let real = std::fs::canonicalize(dir.path()).unwrap();
        assert_eq!(
            validate_download_dir(dir.path().to_str().unwrap()).unwrap(),
            real
        );
        let missing = dir.path().join("nope");
        assert_eq!(
            validate_download_dir(missing.to_str().unwrap()).unwrap_err(),
            format!(
                "download_dir {} -> {}: does not exist",
                py_repr(missing.to_str().unwrap()),
                real.join("nope").display()
            )
        );
        let file = dir.path().join("f");
        std::fs::write(&file, b"").unwrap();
        assert!(
            validate_download_dir(file.to_str().unwrap())
                .unwrap_err()
                .ends_with(": not a directory")
        );
        // `/` belongs to someone else; inside a user namespace it shows as
        // the overflow uid rather than 0, so name whatever owner it has.
        let root_owner = std::os::unix::fs::MetadataExt::uid(&std::fs::metadata("/").unwrap());
        // SAFETY: getuid has no preconditions.
        if unsafe { libc::getuid() } != root_owner {
            assert!(
                validate_download_dir("/")
                    .unwrap_err()
                    .starts_with(&format!(
                        "download_dir /: owned by uid {root_owner}, not us"
                    ))
            );
        }
    }
}
