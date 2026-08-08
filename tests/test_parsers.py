from hermes_medical_search.parsers import parse_pmc_xml, parse_pubmed_xml, reconstruct_abstract


def test_parse_pubmed_xml() -> None:
    xml = """
    <PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>123</PMID><Article>
      <ArticleTitle>A <i>formatted</i> title</ArticleTitle>
      <Abstract><AbstractText Label="BACKGROUND">Useful abstract.</AbstractText></Abstract>
      <AuthorList><Author><ForeName>Ada</ForeName><LastName>Lovelace</LastName></Author></AuthorList>
      <Journal><Title>Journal</Title><JournalIssue><PubDate>
        <Year>2025</Year><Month>Jan</Month>
      </PubDate></JournalIssue></Journal>
      <PublicationTypeList>
        <PublicationType>Randomized Controlled Trial</PublicationType>
      </PublicationTypeList>
    </Article><MeshHeadingList><MeshHeading>
      <DescriptorName>Diabetes Mellitus</DescriptorName>
    </MeshHeading></MeshHeadingList></MedlineCitation>
    <PubmedData><ArticleIdList>
      <ArticleId IdType="pubmed">123</ArticleId>
      <ArticleId IdType="doi">10.1/ABC</ArticleId>
      <ArticleId IdType="pmc">PMC7</ArticleId>
    </ArticleIdList></PubmedData>
    </PubmedArticle></PubmedArticleSet>
    """
    item = parse_pubmed_xml(xml)[0]
    assert item["title"] == "A formatted title"
    assert item["authors"] == ["Ada Lovelace"]
    assert item["publication_date"] == "2025-01"
    assert item["doi"] == "10.1/abc"
    assert item["pmcid"] == "PMC7"


def test_parse_pmc_xml_and_reconstruct_abstract() -> None:
    xml = """
    <article xml:lang="en"><front><journal-meta>
    <journal-title>PMC Journal</journal-title></journal-meta>
    <article-meta><article-id pub-id-type="pmc">42</article-id>
    <article-id pub-id-type="pmid">999</article-id>
    <title-group><article-title>PMC title</article-title></title-group>
    <contrib-group><contrib contrib-type="author"><name>
    <surname>Kim</surname><given-names>J</given-names>
    </name></contrib></contrib-group>
    <pub-date pub-type="epub"><year>2026</year><month>8</month><day>2</day></pub-date>
    <abstract><p>PMC abstract.</p></abstract></article-meta></front></article>
    """
    item = parse_pmc_xml(xml)[0]
    assert item["pmcid"] == "PMC42"
    assert item["pmid"] == "999"
    assert item["publication_date"] == "2026-08-02"
    assert reconstruct_abstract({"world": [1], "Hello": [0]}) == "Hello world"


PUBMED_WITH_REFERENCES = """
<PubmedArticleSet><PubmedArticle>
  <MedlineCitation>
    <PMID>42558455</PMID>
    <Article><ArticleTitle>The actual article</ArticleTitle></Article>
  </MedlineCitation>
  <PubmedData>
    <ArticleIdList>
      <ArticleId IdType="pubmed">42558455</ArticleId>
      <ArticleId IdType="doi">10.3389/fendo.2026.1884596</ArticleId>
      <ArticleId IdType="pmc">PMC13437500</ArticleId>
    </ArticleIdList>
    <ReferenceList>
      <Reference>
        <Citation>Some cited paper</Citation>
        <ArticleIdList>
          <ArticleId IdType="pubmed">18191683</ArticleId>
          <ArticleId IdType="doi">10.1016/S0140-6736(08)60104-X</ArticleId>
          <ArticleId IdType="pmc">PMC8956973</ArticleId>
        </ArticleIdList>
      </Reference>
    </ReferenceList>
  </PubmedData>
</PubmedArticle></PubmedArticleSet>
"""


def test_reference_identifiers_never_overwrite_the_article_identifiers() -> None:
    """PubMed nests an <ArticleIdList> in every <Reference>; only the article's own may win.

    A './/' search returns the cited papers' ids too, and the last one wins — which stamped a
    random reference's DOI onto the record. DOI is the primary deduplication key, so this
    silently merged unrelated papers.
    """
    (record,) = parse_pubmed_xml(PUBMED_WITH_REFERENCES)
    assert record["doi"] == "10.3389/fendo.2026.1884596"
    assert record["pmcid"] == "PMC13437500"
    assert record["pmid"] == "42558455"
    assert record["source_id"] == "42558455"
