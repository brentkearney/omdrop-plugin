//! `airdrop-serve.py`'s command line, with argparse's behaviour: `--opt V`
//! and `--opt=V`, unambiguous prefixes (`--out` for `--outdir`), the last of a
//! repeated option wins, and errors print usage and exit 2.

use std::ffi::OsString;
use std::path::PathBuf;

pub const PROG: &str = "omdrop-receiver";

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Args {
    pub iface: String,
    pub port: i64,
    pub name: Option<String>,
    pub model: Option<String>,
    pub keys: Option<PathBuf>,
    pub outdir: Option<String>,
    pub max_receive_percent: Option<i64>,
    pub config: Option<PathBuf>,
}

/// What to do instead of running.
#[derive(Debug, PartialEq, Eq)]
pub enum Exit {
    /// `-h`: print this to stdout, exit 0.
    Help(String),
    /// A usage error: print this to stderr, exit 2.
    Usage(String),
}

const OPTIONS: [(&str, &str); 8] = [
    ("--iface", "IFACE"),
    ("--port", "PORT"),
    ("--name", "NAME"),
    ("--model", "MODEL"),
    ("--keys", "KEYS"),
    ("--outdir", "OUTDIR"),
    ("--max-receive-percent", "MAX_RECEIVE_PERCENT"),
    ("--config", "CONFIG"),
];

pub fn usage() -> String {
    let opts: Vec<String> = OPTIONS.iter().map(|(o, m)| format!("[{o} {m}]")).collect();
    format!("usage: {PROG} [-h] {}", opts.join(" "))
}

pub fn help(config_path: &str) -> String {
    format!(
        "{}\n\nAirDrop receiver. Config: {config_path} (name, model, download_dir,\nmax_receive_percent, compact_tls); flags override.\n\n\
options:\n  -h, --help            show this help message and exit\n  --iface IFACE\n  --port PORT\n  \
--name NAME           ReceiverComputerName: the label under the tile in the Mac\n                        sheet (config: name)\n  \
--model MODEL         ReceiverModelName: Apple model identifier; picks the\n                        device glyph the sheet draws (config: model)\n  \
--keys KEYS           replace ~/.omdrop for the identity files: DIR/keys holds\n                        the on-disk identity and the self-signed pair (see\n                        docs/identity-contract.md)\n  \
--outdir OUTDIR       where received files go (config: download_dir)\n  \
--max-receive-percent MAX_RECEIVE_PERCENT\n                        maximum transfer size as a percentage of currently\n                        free output disk space\n  \
--config CONFIG       config file path\n",
        usage()
    )
}

fn error(message: String) -> Exit {
    Exit::Usage(format!("{}\n{PROG}: error: {message}\n", usage()))
}

/// Python's `int()` for an argument: surrounding whitespace, a sign, digits.
fn int(option: &str, value: &str) -> Result<i64, Exit> {
    value.trim().parse::<i64>().map_err(|_| {
        error(format!(
            "argument {option}: invalid int value: {}",
            omdrop_proto::archive::py_repr(value)
        ))
    })
}

/// argparse takes `-5` as a value but `-x` as an option.
fn looks_like_option(arg: &str) -> bool {
    arg.starts_with('-') && arg.len() > 1 && arg.parse::<f64>().is_err()
}

pub fn parse(argv: impl IntoIterator<Item = OsString>, config_path: &str) -> Result<Args, Exit> {
    let mut args = Args {
        iface: "awdl0".into(),
        port: 8771,
        name: None,
        model: None,
        keys: None,
        outdir: None,
        max_receive_percent: None,
        config: None,
    };
    let argv: Vec<String> = argv
        .into_iter()
        .skip(1)
        .map(|a| a.to_string_lossy().into_owned())
        .collect();
    let mut unrecognized = Vec::new();
    let mut i = 0;
    while let Some(arg) = argv.get(i) {
        i += 1;
        if arg == "-h" || (arg.len() > 2 && "--help".starts_with(arg.as_str())) {
            return Err(Exit::Help(help(config_path)));
        }
        if arg == "--" {
            unrecognized.extend(argv.get(i..).unwrap_or_default().iter().cloned());
            break;
        }
        if !arg.starts_with("--") || arg.len() == 2 {
            unrecognized.push(arg.clone());
            continue;
        }
        let (flag, inline) = match arg.split_once('=') {
            Some((f, v)) => (f, Some(v.to_string())),
            None => (arg.as_str(), None),
        };
        let matches: Vec<&str> = OPTIONS
            .iter()
            .map(|(o, _)| *o)
            .filter(|o| o.starts_with(flag))
            .collect();
        let option = match matches.as_slice() {
            [one] => *one,
            [] => {
                unrecognized.push(arg.clone());
                continue;
            }
            many => {
                return Err(error(format!(
                    "ambiguous option: {flag} could match {}",
                    many.join(", ")
                )));
            }
        };
        let value = match inline {
            Some(v) => v,
            None => match argv.get(i) {
                Some(v) if !looks_like_option(v) => {
                    i += 1;
                    v.clone()
                }
                _ => return Err(error(format!("argument {option}: expected one argument"))),
            },
        };
        match option {
            "--iface" => args.iface = value,
            "--port" => args.port = int(option, &value)?,
            "--name" => args.name = Some(value),
            "--model" => args.model = Some(value),
            "--keys" => args.keys = Some(PathBuf::from(value)),
            "--outdir" => args.outdir = Some(value),
            "--max-receive-percent" => args.max_receive_percent = Some(int(option, &value)?),
            _ => args.config = Some(PathBuf::from(value)),
        }
    }
    if !unrecognized.is_empty() {
        return Err(error(format!(
            "unrecognized arguments: {}",
            unrecognized.join(" ")
        )));
    }
    Ok(args)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn p(args: &[&str]) -> Result<Args, Exit> {
        parse(
            std::iter::once("omdrop-receiver")
                .chain(args.iter().copied())
                .map(OsString::from),
            "/cfg",
        )
    }

    #[test]
    fn defaults_and_every_option() {
        let a = p(&[]).unwrap();
        assert_eq!(
            (a.iface.as_str(), a.port, a.name, a.config),
            ("awdl0", 8771, None, None)
        );
        let a = p(&[
            "--iface",
            "lo",
            "--port=9000",
            "--name",
            "Drop",
            "--model",
            "Mac14,2",
            "--keys",
            "/k",
            "--outdir",
            "/o",
            "--max-receive-percent",
            "45",
            "--config",
            "/c.toml",
        ])
        .unwrap();
        assert_eq!(a.iface, "lo");
        assert_eq!(a.port, 9000);
        assert_eq!(a.name.as_deref(), Some("Drop"));
        assert_eq!(a.model.as_deref(), Some("Mac14,2"));
        assert_eq!(a.keys, Some(PathBuf::from("/k")));
        assert_eq!(a.outdir.as_deref(), Some("/o"));
        assert_eq!(a.max_receive_percent, Some(45));
        assert_eq!(a.config, Some(PathBuf::from("/c.toml")));
    }

    #[test]
    fn argparse_conveniences() {
        let a = p(&["--out", "/o", "--max", " 7 ", "--port", "1", "--port", "2"]).unwrap();
        assert_eq!(
            (a.outdir.as_deref(), a.max_receive_percent, a.port),
            (Some("/o"), Some(7), 2)
        );
        assert_eq!(
            p(&["--name", "-x"]).unwrap_err(),
            error("argument --name: expected one argument".into())
        );
        assert_eq!(
            p(&["--max-receive-percent", "-5"])
                .unwrap()
                .max_receive_percent,
            Some(-5)
        );
    }

    #[test]
    fn errors_exit_with_usage() {
        let Err(Exit::Usage(text)) = p(&["--port", "x"]) else {
            panic!()
        };
        assert!(text.starts_with("usage: omdrop-receiver [-h] [--iface IFACE]"));
        assert!(
            text.ends_with("omdrop-receiver: error: argument --port: invalid int value: 'x'\n"),
            "{text}"
        );
        let Err(Exit::Usage(text)) = p(&["extra", "--bogus"]) else {
            panic!()
        };
        assert!(text.contains("unrecognized arguments: extra --bogus"));
        let Err(Exit::Usage(text)) = p(&["--port"]) else {
            panic!()
        };
        assert!(text.contains("argument --port: expected one argument"));
        assert!(matches!(p(&["-h"]), Err(Exit::Help(_))));
        assert!(matches!(p(&["--he"]), Err(Exit::Help(_))));
    }
}
