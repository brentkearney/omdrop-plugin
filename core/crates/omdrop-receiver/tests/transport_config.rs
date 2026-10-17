//! Exercise startup validation through the actual receiver binary.
use std::process::Command;

#[test]
fn invalid_transport_settings_refuse_startup_before_creating_an_identity() {
    for (text, error) in [
        ("compact_tls = 1\n", "compact_tls must be a boolean"),
        ("compact_tls = \"true\"\n", "compact_tls must be a boolean"),
        ("min_read_rate = 1024\n", "unknown key(s) min_read_rate"),
    ] {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("config.toml");
        let keys = root.path().join("identity");
        std::fs::write(&path, text).unwrap();
        let result = Command::new(env!("CARGO_BIN_EXE_omdrop-receiver"))
            .arg("--config")
            .arg(&path)
            .arg("--keys")
            .arg(&keys)
            .output()
            .unwrap();
        assert_eq!(result.status.code(), Some(1));
        assert!(String::from_utf8_lossy(&result.stderr).contains(error));
        assert!(!keys.exists());
    }
}

#[test]
fn the_removed_minimum_read_rate_option_is_not_accepted() {
    let result = Command::new(env!("CARGO_BIN_EXE_omdrop-receiver"))
        .args(["--min-read-rate", "1024"])
        .output()
        .unwrap();
    assert_eq!(result.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&result.stderr).contains("unrecognized arguments"));
}
