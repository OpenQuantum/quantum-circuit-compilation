import sys
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))

from prepare_invsqrt_runtime import SKILL_PATH, render_agent_config


BASE = '''[runtime]
storage_path = "runtime/runtime.sqlite3"

[hub]
url = "ws://127.0.0.1:28080"
agent_id = "quantum-agent-local"
world_id = "quantum-world-invsqrt-local"

[human]
allowed_user_id = "local-user"

[generator]
# Existing model settings and comments should remain intact.
model_id = "deepseek/deepseek-v4-pro"
max_rounds = 20

[world_tools]
enabled = true
'''


class InvsqrtSkillConfigTests(unittest.TestCase):
    def setUp(self):
        self.skill = SKILL_PATH.read_text(encoding='utf-8')

    def test_loads_skill_and_preserves_every_other_setting(self):
        updated = render_agent_config(BASE, self.skill, 'General assistant instructions')
        data = tomllib.loads(updated)
        prompt = data['generator'].pop('system_prompt')
        self.assertEqual(data, tomllib.loads(BASE))
        self.assertEqual(prompt, 'General assistant instructions\n\n' + self.skill.strip())
        self.assertIn('# Existing model settings and comments should remain intact.', updated)

    def test_appends_to_custom_multiline_prompt_once(self):
        base = BASE.replace('[generator]\n', '[generator]\nsystem_prompt = """Custom\n[not-a-table]\nprompt  \n"""\n')
        first = render_agent_config(base, self.skill, 'Unused default')
        updated = render_agent_config(first, self.skill, 'Unused default')
        self.assertEqual(first, updated)
        self.assertEqual(
            tomllib.loads(first)['generator']['system_prompt'],
            'Custom\n[not-a-table]\nprompt  \n\n\n' + self.skill.strip(),
        )

    def test_refuses_other_agent_world_or_user(self):
        for old, new in [
            ('quantum-agent-local', 'separate-agent'),
            ('quantum-world-invsqrt-local', 'separate-world'),
            ('local-user', 'separate-user'),
        ]:
            with self.subTest(old=old):
                with self.assertRaisesRegex(ValueError, 'dedicated invsqrt'):
                    render_agent_config(BASE.replace(old, new), self.skill, 'General')

    def test_skill_describes_the_short_user_flow_only(self):
        for required in [
            '请用知识图谱优化 invsqrt', 'GetTask', 'history', 'SearchResearchMethods',
            '"query":"precision"', '"limit":2', '"fallback":true', 'GetResearchNode',
            'method_node_id', 'baseline_full_id', 'H14', '1–3',
            'StartInversqrtOptimization', '详细指令', 'action_wait',
        ]:
            with self.subTest(required=required):
                self.assertIn(required, self.skill)


if __name__ == '__main__':
    unittest.main()
