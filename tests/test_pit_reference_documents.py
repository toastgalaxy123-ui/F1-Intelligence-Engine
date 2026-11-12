import pytest

from f1ri.ingestion.pit_reference_documents import verified_speed_reference


def document():
    return 'FIA 2024 Example Grand Prix\n24 October 2024\nThe pit lane speed limit is 80 km/h for this Event.'


def metadata():
    return {'source_url': 'https://www.fia.com/example-event-notes.pdf', 'season': 2024,
            'circuit_short_name': 'Example', 'source_publication_date': '2024-10-24',
            'publication_date_evidence': '24 October 2024', 'effective_to_utc': '2024-10-28T00:00:00Z'}


def test_verified_speed_keeps_missing_geometry_and_conservative_availability():
    result = verified_speed_reference(document(), metadata(), 'abc123')
    assert result['pit_speed_limit_kph'] == 80
    assert result['layout_version'] is None
    assert result['available_as_of_utc'] == '2024-10-24T23:59:59.999999+00:00'
    assert result['verification_status'] == 'VERIFIED_OFFICIAL_SPEED_ONLY'


def test_ambiguous_document_unverified_event_or_date_is_rejected():
    with pytest.raises(ValueError, match='ambiguous'):
        verified_speed_reference(document()+'\nThe pit lane speed limit is 60 km/h.', metadata(), 'abc')
    with pytest.raises(ValueError, match='event identity'):
        verified_speed_reference(document().replace('Example', 'Elsewhere'), metadata(), 'abc')
    with pytest.raises(ValueError, match='differs'):
        verified_speed_reference(document(), metadata() | {'source_publication_date': '2024-10-23'}, 'abc')


def test_archive_publication_can_delay_but_never_advance_availability():
    later = verified_speed_reference(document(), metadata() | {'archive_publication_date': '2024-10-26'}, 'abc')
    assert later['available_as_of_utc'] == '2024-10-26T23:59:59.999999+00:00'
    earlier = verified_speed_reference(document(), metadata() | {'archive_publication_date': '2024-10-20'}, 'abc')
    assert earlier['available_as_of_utc'] == '2024-10-24T23:59:59.999999+00:00'


def test_real_canadian_practice_decision_distinguishes_limit_from_recorded_speed():
    # Short factual excerpt from FIA 2025 Canadian GP Document 11, June 13.
    text = ('2025 CANADIAN GRAND PRIX\nDate 13 June 2025\n'
            'Fact Pit lane speeding – 80.3 km/h\n'
            'Reason Car 81 exceeded the pit lane speed limit which is set at 80 km/h for this event by 0.3 km/h.')
    md = {'source_url': 'https://www.fia.com/system/files/decision-document/2025_canadian_grand_prix_-_infringement_-_car_81_-_pit_lane_speeding.pdf',
          'season': 2025, 'circuit_short_name': 'Montreal', 'document_circuit_aliases': ['Canadian'],
          'source_publication_date': '2025-06-13', 'publication_date_evidence': '13 June 2025',
          'archive_publication_date': '2025-06-13', 'effective_to_utc': '2025-06-15T20:00:00Z'}
    row = verified_speed_reference(text, md, '610e05222ef80f1160fc6b9baa0fed4871beabbf3f543b94138779397b0baf13')
    assert row['pit_speed_limit_kph'] == 80
    assert '80.3' not in row['source_quote']
