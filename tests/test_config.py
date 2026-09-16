from hermes_medical_research.search.config import Credentials


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


def test_pubmed_and_pmc_are_available_without_optional_contact_configuration() -> None:
    status = Credentials.from_env({}).configuration_status()
    assert status["pubmed"]["configured"] is True
    assert status["pmc"]["configured"] is True
    assert status["pubmed"]["required"] == []
