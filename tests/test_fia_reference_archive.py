from datetime import UTC, datetime

import pytest

from f1ri.ingestion.fia_reference_archive import parse_archive_documents


def archive_row(url, date='13.06.25 18:25'):
    return (f'<li class="document-row key-11"><a href="{url}">'
            '<div class="title">Doc 11 - Infringement - Car 81 - Pit Lane Speeding</div>'
            f'<div class="published"><span class="date-display-single">{date}</span> CET</div></a></li>')


def test_official_archive_parser_records_pre_race_source_and_publication_evidence():
    html = archive_row('/system/files/decision-document/2025_canadian.pdf')
    html += archive_row('/system/files/decision-document/post_race.pdf', '16.06.25 12:00')
    docs = parse_archive_documents(html, 'https://www.fia.com/documents', datetime(2025, 6, 15, 18, tzinfo=UTC))
    assert len(docs) == 1
    assert docs[0]['url'] == 'https://www.fia.com/system/files/decision-document/2025_canadian.pdf'
    assert docs[0]['archive_publication_date'] == '2025-06-13'
    assert docs[0]['archive_published_literal'] == '13.06.25 18:25 CET'


def test_archive_document_link_cannot_leave_official_source_host():
    with pytest.raises(ValueError, match='official HTTPS'):
        parse_archive_documents(archive_row('https://example.org/fia.pdf'), 'https://www.fia.com/documents',
                                datetime(2025, 6, 15, 18, tzinfo=UTC))
