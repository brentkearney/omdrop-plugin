//! TLS for AirDrop, built from the selected [`Identity`].
//!
//! Both directions present our certificate and decide nothing at the TLS layer
//! about who the peer is. An Everyone-mode peer's certificate is self-signed;
//! who signed a sender's certificate is decided by the receiver's Contacts
//! policy, which verifies the chain against Apple's CA itself. So the
//! verifiers below accept any certificate, but they still check every
//! handshake signature: the peer must hold the key for the certificate it
//! presents, or the certificate means nothing to the policy that reads it.
//!
//! TLS 1.2 and 1.3 only. The Python implementations allowed 1.1; no Apple
//! device that speaks AirDrop over AWDL needs it.

use std::fmt;
use std::io;
use std::sync::Arc;

use omdrop_identity::Identity;
use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::crypto::{
    CryptoProvider, WebPkiSupportedAlgorithms, verify_tls12_signature, verify_tls13_signature,
};
use rustls::pki_types::pem::PemObject;
use rustls::pki_types::{CertificateDer, PrivateKeyDer, ServerName, UnixTime};
use rustls::server::danger::{ClientCertVerified, ClientCertVerifier};
use rustls::{
    ClientConfig, DigitallySignedStruct, DistinguishedName, ServerConfig, SignatureScheme,
};

/// Why a TLS configuration could not be built.
#[derive(Debug)]
pub enum TlsError {
    /// The certificate PEM held no certificate, or did not parse.
    Certificate,
    /// The key PEM held no usable private key.
    Key,
    /// rustls refused the pair (for instance, the key does not match).
    Rustls(rustls::Error),
}

impl fmt::Display for TlsError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            TlsError::Certificate => f.write_str("the identity certificate could not be read"),
            TlsError::Key => f.write_str("the identity key could not be read"),
            TlsError::Rustls(e) => write!(f, "the identity was refused: {e}"),
        }
    }
}

impl std::error::Error for TlsError {}

/// The one crypto provider every Omdrop connection uses.
pub fn provider() -> Arc<CryptoProvider> {
    Arc::new(rustls::crypto::ring::default_provider())
}

/// A certificate chain (leaf first) and its private key.
pub type ChainAndKey = (Vec<CertificateDer<'static>>, PrivateKeyDer<'static>);

/// The certificate chain (leaf first) and key of `identity`.
pub fn certified_key(identity: &Identity) -> Result<ChainAndKey, TlsError> {
    let chain: Vec<CertificateDer<'static>> =
        CertificateDer::pem_slice_iter(&identity.certificate_pem)
            .collect::<Result<_, _>>()
            .map_err(|_| TlsError::Certificate)?;
    if chain.is_empty() {
        return Err(TlsError::Certificate);
    }
    let key = PrivateKeyDer::from_pem_slice(&identity.key_pem).map_err(|_| TlsError::Key)?;
    Ok((chain, key))
}

/// Whether the receiver asks the sender for a certificate.
#[derive(Clone, Debug)]
pub enum ClientAuth {
    /// No CertificateRequest at all: Everyone mode.
    None,
    /// Request a certificate, optionally, naming these CAs in the request
    /// (Apple's root and intermediates, as the Python receiver did), and accept
    /// whatever arrives. The Contacts policy reads it after the handshake from
    /// `ServerConnection::peer_certificates()`.
    Request {
        hint_subjects: Vec<DistinguishedName>,
    },
}

/// The receiver's configuration.
pub fn server_config(
    identity: &Identity,
    client_auth: ClientAuth,
) -> Result<Arc<ServerConfig>, TlsError> {
    server_config_with_compact_tls(identity, client_auth, false)
}

/// Restrict receiver key exchange to X25519 when explicitly requested.
/// This excludes P-256/P-384 peers. Ring already prefers X25519 and has no
/// post-quantum groups, so this is not a handshake-size improvement over the
/// current default provider for a peer offering X25519.
pub fn server_config_with_compact_tls(
    identity: &Identity,
    client_auth: ClientAuth,
    compact_tls: bool,
) -> Result<Arc<ServerConfig>, TlsError> {
    let mut provider = rustls::crypto::ring::default_provider();
    if compact_tls {
        provider.kx_groups = vec![rustls::crypto::ring::kx_group::X25519];
    }
    let provider = Arc::new(provider);
    let (chain, key) = certified_key(identity)?;
    let builder = ServerConfig::builder_with_provider(provider.clone())
        .with_safe_default_protocol_versions()
        .map_err(TlsError::Rustls)?;
    let builder = match client_auth {
        ClientAuth::None => builder.with_no_client_auth(),
        ClientAuth::Request { hint_subjects } => {
            builder.with_client_cert_verifier(Arc::new(AnyClientCert {
                algorithms: provider.signature_verification_algorithms,
                hint_subjects,
            }))
        }
    };
    builder
        .with_single_cert(chain, key)
        .map(Arc::new)
        .map_err(TlsError::Rustls)
}

/// The sender's configuration: present our certificate, accept any peer's.
///
/// An Apple-issued leaf goes out with the Apple intermediate that issued it,
/// as a Mac sender's does (our receiver logs a two-certificate chain from
/// one). R7, 2026-10-14: an iPhone in Contacts Only closed every connection
/// from a sender presenting the bare leaf.
pub fn client_config(identity: &Identity) -> Result<Arc<ClientConfig>, TlsError> {
    client_config_with(identity, omdrop_identity::apple::INTERMEDIATES_PEM)
}

/// The chain [`client_config`] presents: our certificate, then its Apple
/// issuer when it has one.
pub fn client_chain(identity: &Identity) -> Result<ChainAndKey, TlsError> {
    client_chain_with(identity, omdrop_identity::apple::INTERMEDIATES_PEM)
}

fn client_chain_with(
    identity: &Identity,
    intermediates: &[&[u8]],
) -> Result<ChainAndKey, TlsError> {
    let (mut chain, key) = certified_key(identity)?;
    if let Some(issuer) = issuing_intermediate(&chain, intermediates) {
        chain.push(issuer);
    }
    Ok((chain, key))
}

fn client_config_with(
    identity: &Identity,
    intermediates: &[&[u8]],
) -> Result<Arc<ClientConfig>, TlsError> {
    let provider = provider();
    let (chain, key) = client_chain_with(identity, intermediates)?;
    ClientConfig::builder_with_provider(provider.clone())
        .with_safe_default_protocol_versions()
        .map_err(TlsError::Rustls)?
        .dangerous()
        .with_custom_certificate_verifier(Arc::new(AnyServerCert {
            algorithms: provider.signature_verification_algorithms,
        }))
        .with_client_auth_cert(chain, key)
        .map(Arc::new)
        .map_err(TlsError::Rustls)
}

/// The certificate among `pems` that issued the last certificate of `chain`,
/// when the chain does not already end in an issuer of its own: the one whose
/// subject is, byte for byte, that certificate's issuer. A self-signed leaf
/// names itself as issuer and gets nothing. Matching names only; no signature
/// is checked, because the receiver verifies the chain anyway.
pub fn issuing_intermediate(
    chain: &[CertificateDer<'static>],
    pems: &[&[u8]],
) -> Option<CertificateDer<'static>> {
    let last = chain.last()?;
    let (issuer, subject) = issuer_and_subject(last)?;
    if issuer == subject {
        return None;
    }
    pems.iter()
        .flat_map(|pem| CertificateDer::pem_slice_iter(pem).filter_map(Result::ok))
        .find(|cand| issuer_and_subject(cand).is_some_and(|(_, s)| s == issuer))
}

/// One DER TLV: (tag, the whole TLV, its content, what follows it).
type Tlv<'a> = (u8, &'a [u8], &'a [u8], &'a [u8]);

fn tlv(b: &[u8]) -> Option<Tlv<'_>> {
    let (&tag, rest) = b.split_first()?;
    let (&first, rest) = rest.split_first()?;
    let (len, rest) = if first < 0x80 {
        (usize::from(first), rest)
    } else {
        let n = usize::from(first & 0x7f);
        if n == 0 || n > 4 || rest.len() < n {
            return None;
        }
        let len = rest[..n]
            .iter()
            .fold(0usize, |a, &d| (a << 8) | usize::from(d));
        (len, &rest[n..])
    };
    if rest.len() < len {
        return None;
    }
    let header = b.len() - rest.len();
    Some((tag, &b[..header + len], &rest[..len], &rest[len..]))
}

/// A certificate's issuer and subject Names, as encoded DER.
fn issuer_and_subject(cert: &[u8]) -> Option<(&[u8], &[u8])> {
    let (0x30, _, cert, _) = tlv(cert)? else {
        return None;
    };
    let (0x30, _, tbs, _) = tlv(cert)? else {
        return None;
    };
    let mut rest = tbs;
    // [0] EXPLICIT version, when present.
    if rest.first() == Some(&0xa0) {
        rest = tlv(rest)?.3;
    }
    let (_, _, _, rest) = tlv(rest)?; // serialNumber
    let (_, _, _, rest) = tlv(rest)?; // signature AlgorithmIdentifier
    let (0x30, issuer, _, rest) = tlv(rest)? else {
        return None;
    };
    let (_, _, _, rest) = tlv(rest)?; // validity
    let (0x30, subject, _, _) = tlv(rest)? else {
        return None;
    };
    Some((issuer, subject))
}

/// The subject names of the certificates in these PEM blobs (Apple's root and
/// intermediates, `omdrop_identity::apple`), for [`ClientAuth::Request`].
pub fn hint_subjects(pems: &[&[u8]]) -> io::Result<Vec<DistinguishedName>> {
    let mut out = Vec::new();
    for pem in pems {
        for cert in CertificateDer::pem_slice_iter(pem) {
            let cert =
                cert.map_err(|e| io::Error::new(io::ErrorKind::InvalidData, format!("{e:?}")))?;
            let anchor = webpki::anchor_from_trusted_cert(&cert)
                .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e.to_string()))?;
            out.push(DistinguishedName::in_sequence(anchor.subject.as_ref()));
        }
    }
    Ok(out)
}

/// The DER the client presented, leaf first, if any.
pub fn peer_chain(conn: &rustls::ServerConnection) -> Vec<CertificateDer<'static>> {
    conn.peer_certificates()
        .map(|c| c.iter().map(|c| c.clone().into_owned()).collect())
        .unwrap_or_default()
}

#[derive(Debug)]
struct AnyServerCert {
    algorithms: WebPkiSupportedAlgorithms,
}

impl ServerCertVerifier for AnyServerCert {
    fn verify_server_cert(
        &self,
        _end_entity: &CertificateDer<'_>,
        _intermediates: &[CertificateDer<'_>],
        _server_name: &ServerName<'_>,
        _ocsp_response: &[u8],
        _now: UnixTime,
    ) -> Result<ServerCertVerified, rustls::Error> {
        Ok(ServerCertVerified::assertion())
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        verify_tls12_signature(message, cert, dss, &self.algorithms)
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        verify_tls13_signature(message, cert, dss, &self.algorithms)
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.algorithms.supported_schemes()
    }
}

#[derive(Debug)]
struct AnyClientCert {
    algorithms: WebPkiSupportedAlgorithms,
    hint_subjects: Vec<DistinguishedName>,
}

impl ClientCertVerifier for AnyClientCert {
    fn offer_client_auth(&self) -> bool {
        true
    }

    fn client_auth_mandatory(&self) -> bool {
        false
    }

    fn root_hint_subjects(&self) -> &[DistinguishedName] {
        &self.hint_subjects
    }

    fn verify_client_cert(
        &self,
        _end_entity: &CertificateDer<'_>,
        _intermediates: &[CertificateDer<'_>],
        _now: UnixTime,
    ) -> Result<ClientCertVerified, rustls::Error> {
        Ok(ClientCertVerified::assertion())
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        verify_tls12_signature(message, cert, dss, &self.algorithms)
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        verify_tls13_signature(message, cert, dss, &self.algorithms)
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.algorithms.supported_schemes()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use omdrop_identity::Source;
    use rustls::{ClientConnection, ServerConnection};
    use std::process::Command;

    fn self_signed(cn: &str, key_spec: &str) -> Identity {
        let dir = tempfile::tempdir().unwrap();
        let mut newkey = vec!["-newkey", key_spec];
        if key_spec == "ec" {
            newkey.extend(["-pkeyopt", "ec_paramgen_curve:prime256v1"]);
        }
        let ok = Command::new("openssl")
            .arg("req")
            .args(&newkey)
            .args([
                "-nodes",
                "-x509",
                "-days",
                "1",
                "-subj",
                &format!("/CN={cn}"),
            ])
            .arg("-keyout")
            .arg(dir.path().join("key.pem"))
            .arg("-out")
            .arg(dir.path().join("certificate.pem"))
            .output()
            .unwrap();
        assert!(
            ok.status.success(),
            "{}",
            String::from_utf8_lossy(&ok.stderr)
        );
        Identity {
            source: Source::SelfSigned,
            certificate_pem: std::fs::read(dir.path().join("certificate.pem")).unwrap(),
            key_pem: std::fs::read(dir.path().join("key.pem")).unwrap().into(),
            record: None,
            warnings: vec![],
        }
    }

    fn handshake(
        server: &mut ServerConnection,
        client: &mut ClientConnection,
    ) -> Result<(), rustls::Error> {
        for _ in 0..20 {
            let mut buf = Vec::new();
            while client.wants_write() {
                client.write_tls(&mut buf).unwrap();
            }
            let mut rd = &buf[..];
            while !rd.is_empty() {
                server.read_tls(&mut rd).unwrap();
                server.process_new_packets()?;
            }
            let mut buf = Vec::new();
            while server.wants_write() {
                server.write_tls(&mut buf).unwrap();
            }
            let mut rd = &buf[..];
            while !rd.is_empty() {
                client.read_tls(&mut rd).unwrap();
                client.process_new_packets()?;
            }
            if !server.is_handshaking() && !client.is_handshaking() {
                return Ok(());
            }
        }
        panic!("handshake did not finish");
    }

    fn connect(
        server_id: &Identity,
        client_id: &Identity,
        auth: ClientAuth,
    ) -> (Result<(), rustls::Error>, ServerConnection) {
        let mut server = ServerConnection::new(server_config(server_id, auth).unwrap()).unwrap();
        let name = ServerName::IpAddress(std::net::Ipv6Addr::LOCALHOST.into());
        let mut client = ClientConnection::new(client_config(client_id).unwrap(), name).unwrap();
        (handshake(&mut server, &mut client), server)
    }

    #[test]
    fn receiver_sees_the_senders_certificate_only_when_it_asks() {
        let receiver = self_signed("receiver", "rsa:2048");
        let sender = self_signed("sender", "ec");
        let (result, server) = connect(
            &receiver,
            &sender,
            ClientAuth::Request {
                hint_subjects: vec![],
            },
        );
        result.unwrap();
        let chain = peer_chain(&server);
        let (sender_chain, _) = certified_key(&sender).unwrap();
        assert_eq!(chain, sender_chain);

        let (result, server) = connect(&receiver, &sender, ClientAuth::None);
        result.unwrap();
        assert!(peer_chain(&server).is_empty());
    }

    #[test]
    fn a_key_that_does_not_match_its_certificate_is_refused() {
        let a = self_signed("a", "rsa:2048");
        let b = self_signed("b", "rsa:2048");
        let mixed = Identity {
            source: Source::Disk,
            certificate_pem: a.certificate_pem.clone(),
            key_pem: b.key_pem.clone(),
            record: None,
            warnings: vec![],
        };
        assert!(matches!(
            server_config(&mixed, ClientAuth::None),
            Err(TlsError::Rustls(_))
        ));
    }

    #[test]
    fn hint_subjects_come_from_the_apple_ca_pems() {
        let id = self_signed("Apple Test CA", "rsa:2048");
        let names = hint_subjects(&[&id.certificate_pem]).unwrap();
        assert_eq!(names.len(), 1);
        assert!(names[0].as_ref().windows(13).any(|w| w == b"Apple Test CA"));
        let apple: Vec<&[u8]> = std::iter::once(omdrop_identity::apple::ROOT_CA_PEM)
            .chain(omdrop_identity::apple::INTERMEDIATES_PEM.iter().copied())
            .collect();
        assert_eq!(hint_subjects(&apple).unwrap().len(), 3);
    }

    /// A CA and a leaf it signed: (CA certificate PEM, leaf identity).
    fn ca_and_leaf() -> (Vec<u8>, Identity) {
        let dir = tempfile::tempdir().unwrap();
        let p = |n: &str| dir.path().join(n);
        let run = |args: &[&str]| {
            let out = Command::new("openssl")
                .args(args)
                .current_dir(dir.path())
                .output()
                .unwrap();
            assert!(
                out.status.success(),
                "{}",
                String::from_utf8_lossy(&out.stderr)
            );
        };
        run(&[
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/O=Test Root/CN=Test Application Integration CA",
            "-keyout",
            "ca.key",
            "-out",
            "ca.pem",
        ]);
        run(&[
            "req",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-nodes",
            "-subj",
            "/CN=com.apple.idms.appleid.prd.test",
            "-keyout",
            "key.pem",
            "-out",
            "leaf.csr",
        ]);
        run(&[
            "x509",
            "-req",
            "-in",
            "leaf.csr",
            "-CA",
            "ca.pem",
            "-CAkey",
            "ca.key",
            "-CAcreateserial",
            "-days",
            "1",
            "-out",
            "certificate.pem",
        ]);
        let id = Identity {
            source: Source::Cache,
            certificate_pem: std::fs::read(p("certificate.pem")).unwrap(),
            key_pem: std::fs::read(p("key.pem")).unwrap().into(),
            record: None,
            warnings: vec![],
        };
        (std::fs::read(p("ca.pem")).unwrap(), id)
    }

    #[test]
    fn a_sender_presents_its_leaf_with_the_intermediate_that_issued_it() {
        let (ca, sender) = ca_and_leaf();
        let other = self_signed("Some Other CA", "rsa:2048");
        let receiver = self_signed("receiver", "rsa:2048");
        let auth = ClientAuth::Request {
            hint_subjects: vec![],
        };
        let mut server = ServerConnection::new(server_config(&receiver, auth).unwrap()).unwrap();
        let name = ServerName::IpAddress(std::net::Ipv6Addr::LOCALHOST.into());
        let config = client_config_with(&sender, &[&other.certificate_pem, &ca]).unwrap();
        let mut client = ClientConnection::new(config, name).unwrap();
        handshake(&mut server, &mut client).unwrap();
        let (leaf, _) = certified_key(&sender).unwrap();
        let ca_der = CertificateDer::pem_slice_iter(&ca).next().unwrap().unwrap();
        assert_eq!(peer_chain(&server), vec![leaf[0].clone(), ca_der]);
    }

    #[test]
    fn a_leaf_with_no_known_issuer_or_self_signed_goes_alone() {
        let (_, sender) = ca_and_leaf();
        let (leaf, _) = certified_key(&sender).unwrap();
        let unrelated = self_signed("Unrelated CA", "rsa:2048");
        assert_eq!(
            issuing_intermediate(&leaf, &[&unrelated.certificate_pem]),
            None
        );
        let (own, _) = certified_key(&unrelated).unwrap();
        assert_eq!(
            issuing_intermediate(&own, &[&unrelated.certificate_pem]),
            None
        );
        assert_eq!(
            issuing_intermediate(&[], &[&unrelated.certificate_pem]),
            None
        );
    }

    #[test]
    fn apples_intermediates_are_matched_to_their_issuer_by_name() {
        // Apple's real DER: each vendored intermediate names Apple's root as
        // its issuer, so the root is what would follow it.
        let root = CertificateDer::pem_slice_iter(omdrop_identity::apple::ROOT_CA_PEM)
            .next()
            .unwrap()
            .unwrap();
        for pem in omdrop_identity::apple::INTERMEDIATES_PEM {
            let int = CertificateDer::pem_slice_iter(pem).next().unwrap().unwrap();
            assert_eq!(
                issuing_intermediate(
                    std::slice::from_ref(&int),
                    &[omdrop_identity::apple::ROOT_CA_PEM]
                ),
                Some(root.clone())
            );
            assert_eq!(
                issuing_intermediate(&[int], omdrop_identity::apple::INTERMEDIATES_PEM),
                None
            );
        }
    }
}
