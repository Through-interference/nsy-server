"""Combine endpoint evidence without certifying an unperformed 3 km field test."""
import argparse
import hashlib
import json
import math
from pathlib import Path


def load_run(directory):
    directory = Path(directory)
    report = json.loads((directory / 'report.json').read_text())
    manifest = json.loads((directory / 'manifest.json').read_text())
    if any(report.get(k) != manifest.get(k) for k in ('mode', 'role', 'session', 'frequencies_mhz', 'link_id')):
        raise ValueError('Report and manifest do not match: ' + str(directory))
    if not {'manifest.json', 'events.jsonl'} <= report['evidence_sha256'].keys():
        raise ValueError('Missing mandatory evidence hashes')
    for name, expected in report['evidence_sha256'].items():
        if Path(name).name != name:
            raise ValueError('Invalid artifact name')
        actual = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError('Evidence hash mismatch: ' + str(directory / name))
    events = [json.loads(line) for line in (directory / 'events.jsonl').read_text().splitlines()]
    return dict(path=str(directory.resolve()), report=report, manifest=manifest, events=events)


def pair(air, ground):
    if air['manifest']['role'] != 'air' or ground['manifest']['role'] != 'ground':
        raise ValueError('Expected one air and one ground run')
    for key in ('mode', 'frequencies_mhz', 'link_id'):
        if air['manifest'][key] != ground['manifest'][key]:
            raise ValueError('Endpoint configuration mismatch: ' + key)
    for local, remote in ((air, ground), (ground, air)):
        if not any(e['event'] in ('link_up', 'link_restored') and e.get('peer_session') == remote['manifest']['session'] for e in local['events']):
            raise ValueError('No matching bidirectional heartbeat evidence')
    return air['manifest']['mode'] == 'radio'


def recovered_channels(local, remote):
    lost = False
    changed, verified, result = set(), set(), set()
    initial = local['manifest']['frequencies_mhz'][0]
    for event in local['events']:
        kind = event['event']
        if kind == 'link_down':
            lost = True;changed.clear();verified.clear()
        elif lost and kind == 'channel_verified':
            verified.add(event['frequency_mhz'])
        elif lost and kind == 'channel_changed':
            if event['from_mhz'] != event['to_mhz']:changed.add(event['to_mhz'])
        elif lost and kind == 'link_restored' and event.get('peer_session') == remote['manifest']['session']:
            frequency = event['frequency_mhz']
            if frequency != initial and frequency in changed and frequency in verified:result.add(frequency)
            lost = False
    return result


def distance(a, g):
    if not a or not g:return None
    lat1, lat2 = math.radians(a['latitude']), math.radians(g['latitude'])
    dlat = lat2 - lat1
    dlon = math.radians(g['longitude'] - a['longitude'])
    h = math.sin(dlat / 2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2)**2
    return 6371000 * 2 * math.asin(min(1, math.sqrt(h)))


def power(run):
    if run['manifest']['mode'] != 'radio':
        return dict(status='not_verified', reason='A localhost test does not read radio power')
    limit = run['manifest'].get('requested_tx_power_limit_dbm')
    measured = [e.get('reported_tx_power_dbm') for e in run['events'] if e['event'] == 'channel_verified']
    site = run['manifest'].get('site') or {}
    if limit is None or not measured or any(v is None for v in measured):
        return dict(status='not_verified', reason='Missing driver power limit/readback')
    if any(v > limit + .01 for v in measured):
        return dict(status='failed', reason='Driver reports power above the requested limit')
    for key in ('antenna_gain_dbi', 'feed_loss_db', 'eirp_limit_dbm'):
        if type(site.get(key)) not in (float, int) or not math.isfinite(site[key]):
            return dict(status='not_verified', reason='Antenna/feed loss/operator EIRP limit not supplied')
    if site['feed_loss_db'] < 0:
        raise ValueError('Feed loss must be nonnegative')
    eirp = max(measured) + site['antenna_gain_dbi'] - site['feed_loss_db']
    return dict(status='within_operator_declared_limit' if eirp <= site['eirp_limit_dbm'] else 'failed',
                estimated_eirp_dbm=eirp, operator_limit_dbm=site['eirp_limit_dbm'],
                caveat='Driver readback and declared antenna data; not an RF power measurement or legal certification')


def evaluate(air, ground, recovery_air=None, recovery_ground=None):
    real_radio = pair(air, ground)
    ra, rg = recovery_air or air, recovery_ground or ground
    recovery_radio = pair(ra, rg)
    reception = ground['report']['reception']
    received = [e for e in ground['events'] if e['event'] == 'payload_received' and e.get('sender_session') == air['manifest']['session']]
    identifiers = {e['sequence'] for e in received}
    if (len(identifiers) != len(received) or len(received) != reception['unique_packets']
            or sum(e['bytes'] for e in received) != reception['payload_bytes']):
        raise ValueError('Payload accounting does not match the receiver journal')
    transmitted = {e['sequence']: e for e in air['events'] if e['event'] == 'payload_sent' and e['submitted']}
    for event in received:
        sent = transmitted.get(event['sequence'])
        if sent is None or sent['bytes'] != event['bytes'] or (sent.get('sha256') is not None and sent['sha256'] != event['sha256']):
            raise ValueError('Received payload has no matching transmitter journal entry')
    duration = reception['measurement_seconds']
    if received:
        duration = max(duration, received[-1]['monotonic_s'] - received[0]['monotonic_s'])
    if reception['source'] == 'generated_load':
        finishes = [e for e in air['events'] if e['event'] == 'benchmark_finished']
        if not finishes or not reception['benchmark_end_received']:
            duration = 0
        else:duration = max(duration, finishes[-1]['duration_s'])
    goodput = 8 * sum(e['bytes'] for e in received) / duration if duration else 0
    throughput = (duration >= 60 and goodput >= 300000
                  and not ground['report']['errors'] and not air['report']['errors'])
    common = recovered_channels(ra, rg) & recovered_channels(rg, ra)
    recovery = bool(common and recovery_radio and not ra['report']['errors'] and not rg['report']['errors'])
    separation = distance(air['manifest'].get('site'), ground['manifest'].get('site'))
    return dict(scope='radio_with_field_review_required' if real_radio else 'localhost_only_no_radio',
                throughput_status='measured_on_radio' if throughput and real_radio else 'localhost_pass_only' if throughput else 'not_proven',
                payload_goodput_bps=goodput, measurement_seconds=duration,
                data_source=reception['source'], loss_fraction=reception['loss_fraction'],
                frequency_recovery_status='readback_and_bidirectional_recovery_observed' if recovery else 'not_proven',
                restored_backup_frequencies_mhz=sorted(common) if recovery else [],
                outage_mechanisms=[e['mechanism'] for run in (ra, rg) for e in run['events'] if e['event'] == 'outage_injected'],
                supplied_coordinate_distance_m=separation, range_status='requires_field_GPS_trace_and_signed_protocol',
                range_target_m=3000, range_target_met_by_supplied_coordinates=separation >= 3000 if separation is not None else None,
                air_power=power(air), ground_power=power(ground),
                other_project_requirements=dict(latency_under_50ms='not_measured', snr_at_least_3db='method_and_radio_measurements_required',
                    two_air_antennas='hardware_evidence_required', aircraft_2_to_3='not_tested',
                    temperature_minus20_to_plus30='not_tested', video_10mbps_or_recognition_alternative='outside_this_telemetry_test'),
                all_requirements_confirmed=False,
                evidence_directories=dict(capacity_air=air['path'], capacity_ground=ground['path'],
                                          recovery_air=ra['path'], recovery_ground=rg['path']))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--air', type=Path, required=True)
    ap.add_argument('--ground', type=Path, required=True)
    ap.add_argument('--recovery-air', type=Path)
    ap.add_argument('--recovery-ground', type=Path)
    ap.add_argument('--output', type=Path, required=True, help='new Markdown protocol, with JSON sidecar')
    args = ap.parse_args()
    if bool(args.recovery_air) != bool(args.recovery_ground):ap.error('Supply both recovery endpoints')
    if args.output.exists() or args.output.with_suffix('.json').exists():ap.error('Output already exists')
    result = evaluate(load_run(args.air), load_run(args.ground),
                      load_run(args.recovery_air) if args.recovery_air else None,
                      load_run(args.recovery_ground) if args.recovery_ground else None)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.with_suffix('.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    separation = result['supplied_coordinate_distance_m']
    text = '# Протокол проверки телеметрии\n\n'
    text += '**Область проверки:** ' + result['scope'] + '.\n\n'
    text += '| Критерий | Результат |\n|---|---|\n'
    text += f"| Полезная скорость ≥300 кбит/с | {result['payload_goodput_bps'] / 1000:.1f} кбит/с; {result['throughput_status']} |\n"
    text += f"| Длительность | {result['measurement_seconds']:.2f} с; источник {result['data_source']} |\n"
    text += f"| Восстановление на резервной частоте | {result['frequency_recovery_status']}; {result['restored_backup_frequencies_mhz']} МГц |\n"
    text += f"| Дальность ≥3000 м | {f'{separation:.1f} м по введённым координатам' if separation is not None else 'Координаты не предоставлены'}; требуется полевое подтверждение |\n"
    text += f"| Мощность борта / земли | {result['air_power']['status']} / {result['ground_power']['status']} |\n\n"
    text += 'Локальный UDP-тест не подтверждает эфир, радиодальность или физическое переключение частоты. '
    text += 'Частота и мощность в журнале — показания драйвера. Введённые координаты не являются независимым измерением.\n\n'
    text += 'Для принятия: приложить GPS/GPX двух точек и время измерения, высоты и поляризацию антенн, '
    text += 'фото стенда/трассы, параметры мощности и ЭИИМ, журналы обоих концов и подписанный полевой протокол.\n\n'
    text += '## Исходные данные\n\n'
    for name, path in result['evidence_directories'].items():text += f'- {name}: `{path}`\n'
    text += '\n**Решение эксперта:** __________. **Дата / подпись:** __________.\n'
    args.output.write_text(text)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':main()
