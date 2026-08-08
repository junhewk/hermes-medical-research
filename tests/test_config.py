from hermes_medical_search.config import Credentials


def test_credentials_are_redacted() -> None:
    credentials = Credentials.from_env(
        {
            "NCBI_EMAIL": "researcher@example.org",
            "NCBI_API_KEY": "ncbi-secret",
            "OPENALEX_API_KEY": "openalex-secret",
            "S2_API_KEY": "s2-secret",
            "SCOPUS_API_KEY": "scopus-secret",
            "SCOPUS_INSTTOKEN": "inst-secret",
        }
    )
    serialized = str(credentials.redacted())
    assert "secret" not in serialized
    assert credentials.configured_sources()[-1] == "scopus"
    assert all(credentials.redacted().values())
