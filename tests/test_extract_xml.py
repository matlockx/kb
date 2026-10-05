"""Structured-format readers: XML legislation formats and GOV.UK Content API JSON."""

import json
from pathlib import Path

import pytest

from kb import extract
from kb.extract import Block

BOE = """<?xml version="1.0" encoding="utf-8"?>
<response><data><texto>
<bloque id="preambulo" tipo="preambulo"><version><p class="parrafo">Sabed</p></version></bloque>
<bloque id="ti" tipo="encabezado" titulo="TÍTULO I"><version>
<p class="titulo_num">TÍTULO I</p><p class="titulo_tit">Objeto</p></version></bloque>
<bloque id="a1" tipo="precepto" titulo="Artículo\u00a01">
<version><p class="articulo">Artículo 1. Objeto.</p><p class="parrafo">Old text.</p></version>
<version><p class="articulo">Artículo 1. Objeto.</p><p class="parrafo">New text.</p>
<blockquote><p class="nota_pie">Se modifica por la Ley 9/2014.</p></blockquote></version></bloque>
<bloque id="fi" tipo="firma"><version><p class="firma_rey">JUAN CARLOS R.</p></version></bloque>
<bloque id="an" tipo="encabezado" titulo="ANEXO"><version><p class="anexo_num">ANEXO</p></version></bloque>
<bloque id="A3" tipo="precepto" titulo="3. Juego"><version><p class="articulo">\u00a0</p>
<p class="articulo">3. Juego.</p><p class="parrafo">3.1 Reglas.\u2013El operador ofrecer\u00e1.</p>
<p class="parrafo">Texto.</p></version></bloque>
</texto></data></response>"""


NORMATTIVA_AKN = """<?xml version="1.0" encoding="UTF-8"?>
<akomaNtoso xmlns="http://docs.oasis-open.org/legaldocml/ns/akn/3.0"><act><body>
<chapter><num>Titolo I</num><heading>REGOLE GENERALI</heading>
<section><num>Capo I</num><heading>Principi</heading>
<article eId="art_1"><num>Art. 1.</num><heading/>
<paragraph><content><p>Finalita'</p></content></paragraph>
<paragraph><num>1.</num><content><p>Le disposizioni.</p></content></paragraph></article>
</section></chapter></body></act></akomaNtoso>"""


def test_akoma_ntoso_articles_under_a_capo_section() -> None:
    assert extract.from_xml(NORMATTIVA_AKN.encode()) == [
        Block("Titolo I REGOLE GENERALI", level=2),
        Block("Capo I Principi", level=3),
        Block("Art. 1. Finalita' 1. Le disposizioni.", ref="Art. 1"),
    ]


def test_boe_reads_latest_version_of_each_precepto() -> None:
    assert extract.from_xml(BOE.encode()) == [
        Block("Sabed"),
        Block("TÍTULO I Objeto", level=1),
        Block("Artículo 1. Objeto.", ref="Artículo 1"),
        Block("New text."),
        Block("ANEXO", level=1),
        Block("3. Juego.", ref="Anexo 3"),
        Block("3.1 Reglas.\u2013El operador ofrecer\u00e1.", ref="Anexo 3.1"),
        Block("Texto."),
    ]


LEXDANIA = """<?xml version="1.0" encoding="utf-8"?>
<Dokument><Meta><DocumentTitle>Lov om spil</DocumentTitle></Meta>
<DokumentIndhold><Kapitel><Explicatus>Kapitel 1</Explicatus><Rubrica>Formål</Rubrica>
<ParagrafGruppe><Rubrica>Lotteri</Rubrica>
<Paragraf><Explicatus>§ 6.</Explicatus>
<Stk><Exitus><Linea><Char>Tilladelse kan gives til Danske Spil A/S.</Char></Linea></Exitus></Stk>
<Stk><Explicatus>Stk. 2.</Explicatus><Exitus><Linea><Char>Tilladelsen kan overdrages.</Char></Linea></Exitus></Stk>
</Paragraf></ParagrafGruppe></Kapitel>
<Ikraft><Exitus><Linea><Char>Lov nr. 1 indeholder følgende ikrafttrædelsesbestemmelse:</Char></Linea></Exitus></Ikraft>
</DokumentIndhold></Dokument>"""


def test_lexdania_marks_paragraphs_and_skips_commencement_notes() -> None:
    assert extract.from_xml(LEXDANIA.encode()) == [
        Block("Kapitel 1 Formål", level=1),
        Block("Lotteri", level=2),
        Block("§ 6. Tilladelse kan gives til Danske Spil A/S.", ref="§ 6"),
        Block("Stk. 2. Tilladelsen kan overdrages."),
    ]


LEXDANIA_AMENDING = """<?xml version="1.0" encoding="UTF-8"?>
<Dokument><Meta><DocumentTitle>Lov om \u00e6ndring af lov om afgifter af spil</DocumentTitle></Meta>
<DokumentIndhold><Hymne><Exitus>Folketinget har vedtaget:</Exitus></Hymne>
<AendringCentreretParagraf><Explicatus>\u00a7 4</Explicatus>
<Exitus>I lov om afgifter af spil foretages f\u00f8lgende \u00e6ndringer:</Exitus>
<AendringsNummer><Explicatus>1.</Explicatus><Aendring><AendringDefinition>
<Exitus>I \u00a7 6, stk. 1, \u00e6ndres \u00bb20 pct.\u00ab til: \u00bb28 pct.\u00ab</Exitus></AendringDefinition>
<AendringAktion><AendringNyTekst/></AendringAktion></Aendring></AendringsNummer>
<AendringsNummer><Explicatus>2.</Explicatus><Aendring><AendringDefinition>
<Exitus>Efter \u00a7 11 inds\u00e6ttes:</Exitus></AendringDefinition><AendringAktion><AendringNyTekst>
<Paragraf><Explicatus>\u00a7 11 a.</Explicatus><Exitus>Afgiften betales m\u00e5nedligt.</Exitus></Paragraf>
</AendringNyTekst></AendringAktion></Aendring></AendringsNummer></AendringCentreretParagraf>
<IkraftCentreretParagraf><Explicatus>\u00a7 9</Explicatus>
<Stk><Exitus>Stk. 1. Loven tr\u00e6der i kraft den 1. januar 2021.</Exitus></Stk></IkraftCentreretParagraf>
</DokumentIndhold></Dokument>"""


def test_lexdania_amending_act_keeps_numbered_amendments_under_their_paragraph() -> None:
    assert extract.from_xml(LEXDANIA_AMENDING.encode()) == [
        Block("\u00a7 4 I lov om afgifter af spil foretages f\u00f8lgende \u00e6ndringer:", ref="\u00a7 4"),
        Block("1. I \u00a7 6, stk. 1, \u00e6ndres \u00bb20 pct.\u00ab til: \u00bb28 pct.\u00ab"),
        Block("2. Efter \u00a7 11 inds\u00e6ttes: \u00a7 11 a. Afgiften betales m\u00e5nedligt."),
        Block("\u00a7 9 Stk. 1. Loven tr\u00e6der i kraft den 1. januar 2021.", ref="\u00a7 9"),
    ]


CLML = """<?xml version="1.0" encoding="UTF-8"?>
<Legislation xmlns="http://www.legislation.gov.uk/namespaces/legislation"><Metadata>ignored</Metadata>
<Primary><Body><Part><Number>Part 4</Number><Title>Operating licences</Title>
<P1group><Title>Nature of licence</Title><P1><Pnumber>65</Pnumber><P1para>
<P2><Pnumber><CommentaryRef Ref="c1"/>1</Pnumber><P2para><Text>The Commission may issue licences.</Text></P2para></P2>
<P2><Pnumber>2</Pnumber><P2para><Text>A licence authorises—</Text>
<P3><Pnumber>j</Pnumber><P3para><Text>to promote a lottery.</Text></P3para></P3></P2para></P2>
</P1para></P1></P1group></Part>
<Schedule><Number>Schedule 18</Number><P1><Pnumber>9</Pnumber><Text>Transitional.</Text></P1></Schedule>
</Body></Primary><Commentaries><Commentary id="c1">note</Commentary></Commentaries></Legislation>"""


def test_clml_marks_sections_and_schedule_paragraphs() -> None:
    assert extract.from_xml(CLML.encode()) == [
        Block("Part 4 Operating licences", level=1),
        Block(
            "Nature of licence 65 (1) The Commission may issue licences. (2) A licence authorises— (j) to promote a "
            "lottery.",
            ref="s. 65",
        ),
        Block("Schedule 18", level=1),
        Block("9 Transitional.", ref="Sch. 18 para. 9"),
    ]


BWB = """<?xml version="1.0" encoding="UTF-8"?><toestand><wetgeving><wet-besluit><wettekst>
<hoofdstuk><kop><label>Hoofdstuk</label><nr>2</nr><titel>De vergunning</titel></kop>
<artikel status="goed"><kop><label>Artikel</label><nr>2.1</nr></kop><meta-data><jcis><jci>x</jci></jcis></meta-data>
<lid><lidnr>1</lidnr><al>De vergunning kan worden verleend voor casinospelen.</al></lid>
<lid><lidnr>2</lidnr><al>De vergunning wordt niet verleend voor loterijen.</al></lid></artikel>
<artikel status="vervallen"><kop><label>Artikel</label><nr>2.2</nr></kop><al>[Vervallen]</al></artikel>
</hoofdstuk></wettekst></wet-besluit></wetgeving></toestand>"""


def test_bwb_marks_articles_and_drops_repealed_ones() -> None:
    assert extract.from_xml(BWB.encode()) == [
        Block("Hoofdstuk 2 De vergunning", level=1),
        Block(
            "Artikel 2.1 1 De vergunning kan worden verleend voor casinospelen. 2 De vergunning wordt niet verleend "
            "voor loterijen.",
            ref="Artikel 2.1",
        ),
    ]


AKN = """<akomaNtoso xmlns="http://docs.oasis-open.org/legaldocml/ns/akn/3.0"><act><meta>skip</meta><body>
<chapter><num>2 luku</num><heading>Toimiluvat</heading>
<section><num>6 §</num><heading>Rahapelitoimilupa</heading>
<subsection><content><p>Seuraaviin toimeenpanomuotoihin voidaan myöntää toimilupa:</p></content></subsection>
</section></chapter></body></act></akomaNtoso>"""


def test_akoma_ntoso_marks_sections() -> None:
    assert extract.from_xml(AKN.encode()) == [
        Block("2 luku Toimiluvat", level=2),
        Block("6 § Rahapelitoimilupa Seuraaviin toimeenpanomuotoihin voidaan myöntää toimilupa:", ref="6 §"),
    ]


def test_unknown_xml_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported XML document <rss>"):
        extract.from_xml(b"<rss/>")


def test_malformed_xml_is_a_value_error() -> None:
    with pytest.raises(ValueError, match="malformed XML"):
        extract.from_xml(b"<?xml version='1.0'?><Dokument>")


def test_extract_dispatches_xml_by_type_or_declaration(tmp_path: Path) -> None:
    path = tmp_path / "doc.bin"
    path.write_bytes(LEXDANIA.encode())
    for content_type in (None, "application/octet-stream", "application/xml", "text/xml"):
        assert extract.extract(path, content_type)[0] == Block("Kapitel 1 Formål", level=1)
    path.write_bytes(BWB.encode().removeprefix(b'<?xml version="1.0" encoding="UTF-8"?>'))
    assert extract.extract(path, "application/xml")[0].level == 1  # typed XML needs no declaration


def test_html_with_an_xml_declaration_stays_html(tmp_path: Path) -> None:
    path = tmp_path / "doc.html"
    path.write_text('<?xml version="1.0"?><html><body><p>Article 135</p></body></html>', encoding="utf-8")
    for content_type in ("application/xhtml+xml", "text/html"):
        assert extract.extract(path, content_type) == [Block("Article 135")]


def test_govuk_content_item_reads_title_and_body(tmp_path: Path) -> None:
    path = tmp_path / "doc.json"
    body = '<h2>1. Rules</h2><p>1.1 Be fair.</p><p class="note">aside</p>'
    path.write_text(json.dumps({"title": "Code", "details": {"body": body}}))
    assert extract.extract(path, "application/json", {"note"}) == [
        Block("Code", 1),
        Block("1. Rules", 2),
        Block("1.1 Be fair."),
    ]
    for bad in ([1], {"details": [1]}, {"details": {"body": 1}}):
        path.write_text(json.dumps(bad))
        with pytest.raises(ValueError, match=r"details\.body"):
            extract.extract(path, "application/json")
    path.write_text("not json")
    with pytest.raises(ValueError):
        extract.extract(path, "application/json")
