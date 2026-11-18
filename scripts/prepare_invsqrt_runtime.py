"""Append the invsqrt skill to an isolated Runtime configuration."""

import argparse
import copy
import json
import re
import tomllib
from pathlib import Path


SKILL_PATH = Path(__file__).resolve().parents[1] / 'skills' / 'invsqrt-graph-optimization.md'
AGENT = 'quantum-agent-local'
WORLD = 'quantum-world-invsqrt-local'
USER = 'local-user'
GENERATOR_TABLE = re.compile(r'(?m)^[ \t]*\[generator\][ \t]*(?:#[^\r\n]*)?\r?\n')
SYSTEM_PROMPT = re.compile(r'(?m)^[ \t]*system_prompt[ \t]*=')


def _rewrite_prompt(text: str, start: int, old: str, replacement: str) -> str:
    for key in SYSTEM_PROMPT.finditer(text, start):
        end = key.start()
        for line in text[end:].splitlines(keepends=True):
            end += len(line)
            current = text[key.start():end]
            try:
                parsed = tomllib.loads(current)
            except tomllib.TOMLDecodeError:
                continue
            if parsed == {'system_prompt': old}:
                ending = '\r\n' if current.endswith('\r\n') else '\n' if current.endswith('\n') else ''
                return text[:key.start()] + replacement + ending + text[end:]
            break
    raise ValueError('Could not locate an existing system_prompt in [generator]')


def render_agent_config(text: str, skill: str, default_prompt: str) -> str:
    config = tomllib.loads(text)
    hub, human = config.get('hub', {}), config.get('human', {})
    if (hub.get('agent_id'), hub.get('world_id'), human.get('allowed_user_id')) != (AGENT, WORLD, USER):
        raise ValueError('The prompt is scoped to the dedicated invsqrt Agent, World and user')
    table = GENERATOR_TABLE.search(text)
    if not table:
        raise ValueError('The dedicated Runtime configuration must have an explicit [generator] table')
    generator = config['generator']
    conversation = config.get('conversation', {})
    if 'api_key' in generator or {'decision_api_key', 'live_api_key'} & conversation.keys():
        raise ValueError('Configure model credentials through the existing process environment')
    old = generator.get('system_prompt', default_prompt)
    skill = skill.strip()
    if not skill or not isinstance(old, str):
        raise ValueError('A valid system prompt and invsqrt skill are required')
    if old == skill or old.endswith('\n\n' + skill):
        return text
    updated = f'{old}\n\n{skill}' if old else skill
    line = 'system_prompt = ' + json.dumps(updated, ensure_ascii=False)
    if 'system_prompt' in generator:
        result = _rewrite_prompt(text, table.end(), old, line)
    else:
        ending = '\r\n' if table.group().endswith('\r\n') else '\n'
        result = text[:table.end()] + line + ending + text[table.end():]
    expected = copy.deepcopy(config)
    expected['generator']['system_prompt'] = updated
    if tomllib.loads(result) != expected:
        raise ValueError('Preparing the invsqrt prompt changed an unrelated Runtime setting')
    return result


def main() -> None:
    from agent_runtime.config.settings import GeneratorSettings, load_settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    base, output = args.base_config.resolve(), args.output.resolve()
    if base == output or base.parent != output.parent or '.local-console' not in output.parts:
        parser.error('output must be a separate file next to the base config in .local-console')

    text = base.read_bytes().decode('utf-8')
    skill = SKILL_PATH.read_text(encoding='utf-8')
    prepared = render_agent_config(text, skill, GeneratorSettings().system_prompt)
    if output.exists():
        if output.read_bytes().decode('utf-8') != prepared:
            parser.error('output already exists with different content')
    else:
        with output.open('x', encoding='utf-8', newline='') as stream:
            stream.write(prepared)
    load_settings(output)
    print(f'Dedicated invsqrt Runtime configuration is valid: {output}')


if __name__ == '__main__':
    main()
