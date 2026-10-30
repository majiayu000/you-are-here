#!/usr/bin/env python3
"""Read Claude session logs and one Codex turn; write aggregate facts, never transcript bodies."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path


TOKEN_KEYS = ('input', 'cache_read', 'cache_write', 'output', 'reasoning')


def rows(path):
    with Path(path).open(encoding='utf-8') as stream:
        for number, line in enumerate(stream, 1):
            if line.strip():
                try:
                    yield number, json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f'{path}:{number}: invalid JSONL') from exc


def tokens(usage, provider):
    output = usage.get('output_tokens', 0)
    cache = usage.get('cache_read_input_tokens' if provider == 'claude' else 'cached_input_tokens', 0)
    reasoning = (usage.get('output_tokens_details') or {}).get('thinking_tokens', 0) if provider == 'claude' else usage.get('reasoning_output_tokens', 0)
    # Codex input includes cache; Claude's three input fields are disjoint.
    write = usage.get('cache_creation_input_tokens' if provider == 'claude' else 'cache_write_input_tokens', 0)
    return {'input': usage.get('input_tokens', 0) - (cache + write if provider == 'codex' else 0),
            'cache_read': cache,
            'cache_write': write,
            'output': output, 'reasoning': reasoning}


def total(items):
    result = {k: sum(x.get(k, 0) for x in items) for k in TOKEN_KEYS}
    # Reasoning is already part of output; do not add it twice.
    result['total'] = sum(result[k] for k in ('input', 'cache_read', 'cache_write', 'output'))
    inputs = result['input'] + result['cache_read'] + result['cache_write']
    result['cache_ratio'] = result['cache_read'] / inputs if inputs else None
    return result


def summarize_requests(requests):
    models = {}
    for item in requests.values():
        models.setdefault(item['model'], []).append(item['tokens'])
    return {'requests': len(requests), 'tokens': total([x['tokens'] for x in requests.values()]),
            'models': [{'name': name, 'requests': len(items), 'tokens': total(items)['total']}
                       for name, items in sorted(models.items())]}


def claude_session(path):
    path = Path(path).resolve()
    children = sorted(path.with_suffix('').joinpath('subagents').glob('*.jsonl'))
    requests, stamps, missing_usage, raw_usage = {}, [], set(), 0
    for log in [path, *children]:
        for line, row in rows(log):
            kind, message = row.get('type'), row.get('message') or {}
            if log == path and row.get('timestamp') and kind in ('user', 'assistant', 'system'):
                stamps.append(row['timestamp'])
            if kind != 'assistant' or message.get('model') == '<synthetic>':
                continue
            key, usage = message.get('id'), message.get('usage')
            if key and usage:
                raw_usage += 1
                requests[key] = {'model': message.get('model', 'unknown'), 'tokens': tokens(usage, 'claude')}
            elif key:
                missing_usage.add(key)
    return {'provider': 'Claude Code', 'session_id': path.stem, 'source': str(path),
            'start': min(stamps) if stamps else None, 'end': max(stamps) if stamps else None,
            'subagents': len(children), 'usage_available': bool(requests),
            'raw_usage_rows': raw_usage, 'duplicate_usage_rows': raw_usage - len(requests),
            'missing_usage_requests': len(missing_usage - requests.keys()), **summarize_requests(requests)}


def codex_turn(path, turn_id):
    requests = {}
    active, model, started, ended = False, 'unknown', None, None
    last_stamp, token_count_rows, completed, raw_usage = None, 0, False, 0
    cumulative = None
    for line, row in rows(path):
        kind, data, stamp = row.get('type'), row.get('payload') or {}, row.get('timestamp')
        event = data.get('type')
        if kind == 'turn_context':
            active = data.get('turn_id') == turn_id
            model = data.get('model', model)
        if kind == 'event_msg' and event == 'task_started':
            active = data.get('turn_id') == turn_id
            if active:
                started = stamp
        if active and stamp:
            last_stamp = stamp
        if kind == 'token_usage_record' and data.get('turn_id') == turn_id:
            key = data.get('response_id')
            if key and data.get('usage'):
                raw_usage += 1
                requests[key] = {'model': model, 'tokens': tokens(data['usage'], 'codex'), 'line': line}
                cumulative = (data.get('turn_token_usage') or {}).get('total_tokens')
        if not active:
            continue
        if kind == 'event_msg' and event == 'token_count':
            token_count_rows += 1  # display coverage; never add the cumulative counter
        if kind == 'event_msg' and event == 'task_complete' and data.get('turn_id') == turn_id:
            ended, completed = stamp, True
            active = False
    if not started:
        raise ValueError(f'turn {turn_id} has no task_started boundary in {path}')
    result = summarize_requests(requests)
    return {'provider': 'Codex', 'source': str(Path(path).resolve()), 'turn_id': turn_id,
            'start': started, 'end': ended or last_stamp, 'complete': completed,
            'raw_usage_rows': raw_usage,
            'duplicate_usage_rows': raw_usage - len(requests), 'ignored_cumulative_rows': token_count_rows,
            'turn_counter_total': cumulative,
            'counter_matches': result['tokens']['total'] == cumulative if cumulative is not None else None,
            'usage_available': bool(requests), **result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, help='Original Claude JSONL; includes its subagents directory')
    parser.add_argument('--run', required=True, help='Codex JSONL for the map generation')
    parser.add_argument('--turn', required=True, help='Exact Codex turn ID; never the whole thread')
    parser.add_argument('--out', required=True, help='Output JSON inside the map output directory')
    args = parser.parse_args()
    report = {'generated_at': datetime.now(timezone.utc).isoformat(),
              'original': claude_session(args.source), 'skill_run': codex_turn(args.run, args.turn),
              'notes': ['总量包含缓存读取；推理 Token 已计入输出，不重复相加。',
                        '原始会话包含其保留的子任务日志，按消息 ID 去重。',
                        '本轮 Skill 使用包含该调用回合的读取、分析、绘图与校验；不含 Skill 自身的开发迭代。',
                        '数字来自日志中的逐请求用量，不是账单；缺失用量不作估算。']}

    destination = Path(args.out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: {'tokens': v['tokens']['total'], 'requests': v['requests']} for k, v in report.items() if k in ('original', 'skill_run')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
