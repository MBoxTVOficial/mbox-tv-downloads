"""Shared safe CLI diagnostics; no providers, network or credentials."""
import json


def emit_result(feed, report, changed, requested_date, *, public=False):
    available = feed.get('date', 'none') if feed else 'none'
    events = sum(len(s.get('events', [])) for s in (feed or {}).get('sections', []))
    # Only whitelisted codes and fixed config names enter diagnostics/annotations.
    for source in report.get('sources', []):
        print('SPORTS_AGENDA SOURCE ' + json.dumps(source, ensure_ascii=True))
        if source['state'] in {'ERROR', 'ERROR_USING_CACHE', 'REVIEW'}:
            print('::warning title=Sports agenda source::provider=' + source.get('provider', source.get('source', 'unknown')) +
                  ' reason=' + source.get('reason', source['state']))
    for guide in report.get('guides', []):
        print('SPORTS_AGENDA GUIDE ' + json.dumps(guide, ensure_ascii=True))
    if public:
        print('SPORTS_AGENDA PUBLIC requests=' + str(report.get('publicRequests', 0)) +
              ' sportsApiRequests=' + str(report.get('sportsApiRequests', 0)))
    print('SPORTS_AGENDA state=' + report['state'] + ' changed=' + str(changed).lower() +
          ' requestedDate=' + requested_date.isoformat() + ' availableDate=' + available +
          ' reason=' + report.get('reason', 'OK') + ' stage=' + report.get('stage', 'COMPLETE') +
          ' events=' + str(events) + ' requests=' + json.dumps(report.get('requests', {}), sort_keys=True))
    if available != requested_date.isoformat():
        print('::error title=Sports agenda unavailable::requestedDate=' + requested_date.isoformat() +
              ' availableDate=' + available + ' reason=' + report.get('reason', 'NO_CURRENT_FEED'))
        return 1
    return 0
