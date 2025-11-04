# MOMOKA/llm/plugins/commands_manager.py
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, List, Dict, Any

import discord
from discord.ext import commands

if TYPE_CHECKING:
    from discord.ext.commands import Bot

logger = logging.getLogger(__name__)


class CommandInfoManager:
    """Botの全コマンド情報を収集・整形するマネージャー"""

    def __init__(self, bot: Bot):
        self.bot = bot
        logger.info("CommandInfoManager initialized.")

    def get_all_commands_info(self) -> str:
        """
        _cog.pyで終わるCogから全コマンドを収集し、
        LLMに渡すための整形されたテキストを返す

        Returns:
            str: コマンド情報を整形したテキスト
        """
        commands_text = "# 🤖 利用可能なBotコマンド一覧\n\n"
        commands_text += (
            "ユーザーが特定の機能を求めたり、コマンドの使い方を尋ねた場合は、"
            "以下のコマンド一覧から**最も関連性の高いコマンド**を提案してください。\n\n"
            "**提案時の注意点:**\n"
            "- コマンド名、説明、使用例を**明確に表示**してください\n"
            "- ユーザーの要求に最も適したコマンドを1〜3個提案してください\n"
            "- 必要に応じてパラメータの説明も追加してください\n\n"
        )

        # スラッシュコマンドを収集
        slash_commands = self._collect_slash_commands_from_cog_files()

        if slash_commands:
            # カテゴリ（Cog名）ごとにグループ化
            categorized = {}
            for cmd_info in slash_commands:
                category = cmd_info.get('cog', 'その他')
                if category not in categorized:
                    categorized[category] = []
                categorized[category].append(cmd_info)

            for category, commands in sorted(categorized.items()):
                commands_text += f"## 📁 {category}\n\n"
                for cmd_info in commands:
                    commands_text += self._format_command_info_detailed(cmd_info)
                commands_text += "\n"
        else:
            commands_text += "現在利用可能なコマンドはありません。\n"

        return commands_text

    def _collect_slash_commands_from_cog_files(self) -> List[Dict[str, Any]]:
        """_cog.pyで終わるファイルからスラッシュコマンドを収集"""
        commands_list = []
        loaded_cog_names = set()

        # ロード済みのCogのうち、_cog.pyで終わるものを特定
        for ext_name in self.bot.extensions.keys():
            # 例: PLANA.music.music_cog -> music_cog
            module_parts = ext_name.split('.')
            if module_parts[-1].endswith('_cog'):
                loaded_cog_names.add(module_parts[-1])

        logger.info(f"🔍 [CommandInfoManager] Found {len(loaded_cog_names)} _cog.py files: {loaded_cog_names}")

        # グローバルコマンド
        all_global_commands = list(self.bot.tree.get_commands())
        logger.info(f"🔍 [CommandInfoManager] Found {len(all_global_commands)} global commands")

        for command in all_global_commands:
            # Groupオブジェクトの場合はスキップ
            if command.__class__.__name__ == 'Group':
                logger.debug(f"Skipping Group object: {command.name}")
                continue

            logger.debug(f"Processing command: {command.name} (type: {command.__class__.__name__})")

            # _cog.pyからのコマンドかチェック（チェックを緩める）
            if hasattr(command, 'binding') and command.binding:
                cog_name = command.binding.__class__.__name__
                logger.debug(f"  -> Cog: {cog_name}")

                # よりシンプルな判定: 'Cog'で終わるか、loaded_cog_namesに含まれれば収集
                if 'cog' in cog_name.lower() or any(name in cog_name.lower() for name in loaded_cog_names):
                    cmd_info = self._extract_slash_command_info(command)
                    if cmd_info:
                        commands_list.append(cmd_info)
                        #logger.info(f"  ✅ Collected: /{cmd_info['name']} from {cmd_info['cog']}")
                else:
                    logger.debug(f"  ❌ Skipped: {cog_name} doesn't match criteria")
            else:
                logger.debug(f"  ❌ Skipped: No binding or binding is None")

        # ギルド固有のコマンド
        for guild in self.bot.guilds:
            for command in self.bot.tree.get_commands(guild=guild):
                # Groupオブジェクトの場合はスキップ
                if command.__class__.__name__ == 'Group':
                    logger.debug(f"Skipping Group object: {command.name}")
                    continue

                if hasattr(command, 'binding') and command.binding:
                    cog_name = command.binding.__class__.__name__
                    if 'cog' in cog_name.lower() or any(name in cog_name.lower() for name in loaded_cog_names):
                        cmd_info = self._extract_slash_command_info(command)
                        if cmd_info and cmd_info not in commands_list:
                            commands_list.append(cmd_info)
                            logger.info(f"  ✅ Collected (guild): /{cmd_info['name']} from {cmd_info['cog']}")

        logger.info(f"🔍 [CommandInfoManager] Total collected: {len(commands_list)} commands")
        return commands_list

    def _is_command_from_target_cog(self, command, target_cog_names: set) -> bool:
        """コマンドが_cog.pyのCogから来ているかチェック"""
        # Groupオブジェクトの場合はbinding属性がないのでスキップ
        if not hasattr(command, 'binding'):
            return False

        if not command.binding:
            return False

        cog_class_name = command.binding.__class__.__name__

        # Cog名が_cogで終わるか、target_cog_namesに含まれるかチェック
        if cog_class_name.endswith('Cog') or cog_class_name.lower() in target_cog_names:
            return True

        return False

    def _extract_slash_command_info(self, command) -> Dict[str, Any]:
        """スラッシュコマンドから詳細情報を抽出"""
        try:
            cmd_info = {
                'name': command.name,
                'description': command.description or "説明なし",
                'parameters': [],
                'cog': command.binding.__class__.__name__ if command.binding else 'Unknown',
                'usage_examples': []
            }

            # パラメータ情報を抽出
            if hasattr(command, 'parameters'):
                for param in command.parameters:
                    param_info = {
                        'name': param.name,
                        'description': param.description or '',
                        'required': param.required,
                        'type': self._get_param_type_name(param.type)
                    }

                    # 選択肢がある場合
                    if hasattr(param, 'choices') and param.choices:
                        param_info['choices'] = [
                            {'name': choice.name, 'value': choice.value}
                            for choice in param.choices
                        ]

                    cmd_info['parameters'].append(param_info)

            # 使用例を生成
            cmd_info['usage_examples'] = self._generate_usage_examples(cmd_info)

            return cmd_info
        except Exception as e:
            logger.warning(f"Failed to extract info from slash command: {e}")
            return None

    def _get_param_type_name(self, param_type) -> str:
        """パラメータの型名を取得"""
        if hasattr(param_type, 'name'):
            return param_type.name
        elif hasattr(param_type, '__name__'):
            return param_type.__name__
        else:
            type_str = str(param_type)
            # <class 'str'> -> str のような変換
            if "'" in type_str:
                return type_str.split("'")[1].split(".")[-1]
            return type_str

    def _generate_usage_examples(self, cmd_info: Dict[str, Any]) -> List[str]:
        """コマンドの使用例を自動生成"""
        examples = []
        base_cmd = f"/{cmd_info['name']}"

        if not cmd_info['parameters']:
            examples.append(base_cmd)
            return examples

        # 必須パラメータのみの例
        required_params = [p for p in cmd_info['parameters'] if p['required']]
        if required_params:
            example_parts = [base_cmd]
            for param in required_params:
                example_value = self._get_example_value(param)
                example_parts.append(f"{param['name']}: {example_value}")
            examples.append(" ".join(example_parts))

        # 全パラメータを使った例
        if len(cmd_info['parameters']) > len(required_params):
            example_parts = [base_cmd]
            for param in cmd_info['parameters']:
                example_value = self._get_example_value(param)
                example_parts.append(f"{param['name']}: {example_value}")
            examples.append(" ".join(example_parts))

        return examples

    def _get_example_value(self, param: Dict[str, Any]) -> str:
        """パラメータの例示値を生成"""
        if 'choices' in param and param['choices']:
            return param['choices'][0]['name']

        param_type = param['type'].lower()
        param_name = param['name'].lower()

        # 型に応じた例示値
        if 'url' in param_name or param_type == 'string' and 'link' in param['description'].lower():
            return "https://example.com"
        elif 'number' in param_type or 'int' in param_type:
            return "1"
        elif 'bool' in param_type:
            return "True"
        elif param_type == 'string':
            # パラメータ名から推測
            if 'query' in param_name or 'search' in param_name:
                return "検索キーワード"
            elif 'message' in param_name or 'text' in param_name:
                return "メッセージ内容"
            elif 'name' in param_name:
                return "名前"
            else:
                return "値"
        else:
            return "..."

    def _format_command_info_detailed(self, cmd_info: Dict[str, Any]) -> str:
        """コマンド情報を詳細に整形"""
        text = f"### /{cmd_info['name']}\n"
        text += f"**説明**: {cmd_info['description']}\n"

        if cmd_info['parameters']:
            text += "**パラメータ**:\n"
            for param in cmd_info['parameters']:
                required_mark = "🔴 必須" if param['required'] else "⚪ オプション"
                text += f"  - `{param['name']}` ({param['type']}) {required_mark}\n"
                if param['description']:
                    text += f"    └ {param['description']}\n"

                if 'choices' in param:
                    choices_str = ", ".join([f"`{c['name']}`" for c in param['choices'][:5]])
                    text += f"    └ 選択肢: {choices_str}\n"

        if cmd_info['usage_examples']:
            text += "**使用例**:\n"
            for example in cmd_info['usage_examples']:
                text += f"  `{example}`\n"

        text += "\n"
        return text

    def search_commands_by_keywords(self, keywords: List[str]) -> List[Dict[str, Any]]:
        """
        キーワードでコマンドを検索（LLMが内部で使用可能）

        Args:
            keywords: 検索キーワードのリスト（例: ["音楽", "再生"]）

        Returns:
            マッチしたコマンド情報のリスト
        """
        all_commands = self._collect_slash_commands_from_cog_files()
        matches = []

        for cmd in all_commands:
            cmd_text = f"{cmd['name']} {cmd['description']}".lower()

            # いずれかのキーワードがマッチすればOK
            if any(keyword.lower() in cmd_text for keyword in keywords):
                matches.append(cmd)

        return matches

    def get_commands_by_category(self, category: str) -> str:
        """
        特定のカテゴリ（Cog名）のコマンドのみを取得

        Args:
            category: Cog名

        Returns:
            str: 該当カテゴリのコマンド情報
        """
        all_commands = self._collect_slash_commands_from_cog_files()
        filtered = [cmd for cmd in all_commands if cmd.get('cog', '').lower() == category.lower()]

        if not filtered:
            return f"カテゴリ '{category}' のコマンドは見つかりませんでした。\n"

        text = f"# {category} のコマンド\n\n"
        for cmd_info in filtered:
            text += self._format_command_info_detailed(cmd_info)

        return text