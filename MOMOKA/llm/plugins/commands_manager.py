# MOMOKA/llm/plugins/commands_manager.py
from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING, List, Dict, Any, Optional

import discord
from discord.ext import commands

if TYPE_CHECKING:
    from discord.ext.commands import Bot

logger = logging.getLogger(__name__)

# 日本語文字（ひらがな・カタカナ・漢字・全角記号）を検出する正規表現パターン
_JAPANESE_CHAR_RE = re.compile(
    r'[\u3000-\u303F\u3040-\u309F\u30A0-\u30FF'
    r'\u4E00-\u9FFF\uF900-\uFAFF\u3400-\u4DBF\uFF00-\uFFEF]'
)


class CommandInfoManager:
    """
    Botの全コマンド情報を収集し、LLMツールとして提供するマネージャー。

    LLMがユーザーにコマンドの説明を求められた場合にのみ呼び出される。
    システムプロンプトには注入しないため、言語バイアスを回避できる。
    """

    # ツール名（LLMから呼び出される関数名）
    name = "get_commands_info"

    # OpenAI function-calling 形式のツール定義
    tool_spec = {
        "type": "function",
        "function": {
            "name": name,
            "description": (
                "Retrieve a list of all available bot commands with descriptions, "
                "parameters, and usage examples. Call this tool ONLY when the user "
                "asks about available commands, how to use a command, or needs help "
                "finding the right command for their goal."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Optional keyword to filter commands "
                            "(e.g. 'music', 'image', 'dice'). "
                            "Leave empty to get all commands."
                        ),
                    }
                },
                "required": [],
            },
        },
    }

    def __init__(self, bot: Bot):
        self.bot = bot
        logger.info("CommandInfoManager initialized.")

    # ==================================================================
    # 英語テキスト抽出ヘルパー
    # ==================================================================
    @staticmethod
    def _extract_english_text(text: str) -> str:
        """
        バイリンガルテキストから英語部分のみを抽出する。

        対応パターン:
          1. "English\\nJapanese"  → English 部分を返す
          2. "Japanese\\nEnglish"  → English 部分を返す
          3. "Japanese / English"  → English 部分を返す（スラッシュ前後の空白は柔軟に許容）
          4. "English / Japanese"  → English 部分を返す
          5. 英語のみ             → そのまま返す
          6. 日本語のみ           → そのまま返す（フォールバック）

        Args:
            text: 対象テキスト

        Returns:
            str: 英語部分のテキスト
        """
        if not text:
            return text

        # --- パターン1&2: 改行区切り ---
        if '\n' in text:
            lines = text.split('\n')
            # 各行が日本語を含むか判定し、英語行だけ収集
            english_lines = [
                line.strip() for line in lines
                if line.strip() and not _JAPANESE_CHAR_RE.search(line)
            ]
            if english_lines:
                return ' '.join(english_lines)

        # --- パターン3&4: スラッシュ区切り（前後の空白を柔軟に許容） ---
        if '/' in text:
            parts = re.split(r'\s*/\s*', text)
            # 日本語を含まないパートだけ収集
            english_parts = [
                part.strip() for part in parts
                if part.strip() and not _JAPANESE_CHAR_RE.search(part)
            ]
            if english_parts:
                return ' / '.join(english_parts)

        # --- パターン5&6: 分離できない場合はそのまま返す ---
        return text.strip()

    # ==================================================================
    # ツール実行エントリーポイント（LLMから呼び出される）
    # ==================================================================
    async def run(self, arguments: Dict[str, Any], **kwargs) -> str:
        """
        LLMツールとして呼び出された際のエントリーポイント。

        Args:
            arguments: ツール引数（"query" キーにフィルタ用キーワード）

        Returns:
            str: 整形されたコマンド情報テキスト（英語）
        """
        # Bot準備完了まで待機
        await self.bot.wait_until_ready()

        query = arguments.get("query", "").strip()

        if query:
            # キーワード指定時はフィルタリング検索
            logger.info(f"🔍 [CommandInfoManager] Tool called with query='{query}'")
            return self._get_filtered_commands_info(query)
        else:
            # キーワードなし → 全コマンド一覧
            logger.info("🔍 [CommandInfoManager] Tool called (all commands)")
            return self.get_all_commands_info()

    # ==================================================================
    # メイン: 全コマンド情報を収集（英語のみ）
    # ==================================================================
    def get_all_commands_info(self) -> str:
        """
        _cog.pyで終わるCogから全コマンドを収集し、
        LLMに渡すための整形されたテキスト（英語のみ）を返す。

        Returns:
            str: コマンド情報を整形したテキスト（英語）
        """
        # ヘッダーと指示文を英語で構成
        commands_text = "# Available Bot Commands\n\n"
        commands_text += (
            "Below is the full list of commands. "
            "Present the most relevant ones to the user.\n\n"
        )

        # スラッシュコマンドを収集
        slash_commands = self._collect_slash_commands_from_cog_files()

        if slash_commands:
            # カテゴリ（Cog名）ごとにグループ化
            categorized: Dict[str, List[Dict[str, Any]]] = {}
            for cmd_info in slash_commands:
                category = cmd_info.get('cog', 'Other')
                if category not in categorized:
                    categorized[category] = []
                categorized[category].append(cmd_info)

            for category, cmds in sorted(categorized.items()):
                commands_text += f"## {category}\n\n"
                for cmd_info in cmds:
                    commands_text += self._format_command_info_detailed(cmd_info)
                commands_text += "\n"
        else:
            commands_text += "No commands are currently available.\n"

        return commands_text

    # ==================================================================
    # フィルタリング検索
    # ==================================================================
    def _get_filtered_commands_info(self, query: str) -> str:
        """
        キーワードでコマンドをフィルタし、マッチしたものだけ整形して返す。

        Args:
            query: 検索キーワード

        Returns:
            str: マッチしたコマンド情報（英語）
        """
        keywords = query.lower().split()
        all_commands = self._collect_slash_commands_from_cog_files()
        matches = []

        for cmd in all_commands:
            # コマンド名・説明を検索対象にする
            cmd_text = f"{cmd['name']} {cmd['description']}".lower()
            if any(kw in cmd_text for kw in keywords):
                matches.append(cmd)

        if not matches:
            return f"No commands found matching '{query}'."

        text = f"# Commands matching '{query}'\n\n"
        for cmd_info in matches:
            text += self._format_command_info_detailed(cmd_info)

        return text

    # ==================================================================
    # スラッシュコマンド収集
    # ==================================================================
    def _collect_slash_commands_from_cog_files(self) -> List[Dict[str, Any]]:
        """_cog.pyで終わるファイルからスラッシュコマンドを収集"""
        commands_list = []
        loaded_cog_names = set()

        # ロード済みのCogのうち、_cog.pyで終わるものを特定
        for ext_name in self.bot.extensions.keys():
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

            # _cog.pyからのコマンドかチェック
            if hasattr(command, 'binding') and command.binding:
                cog_name = command.binding.__class__.__name__
                logger.debug(f"  -> Cog: {cog_name}")

                if 'cog' in cog_name.lower() or any(name in cog_name.lower() for name in loaded_cog_names):
                    cmd_info = self._extract_slash_command_info(command)
                    if cmd_info:
                        commands_list.append(cmd_info)
                else:
                    logger.debug(f"  ❌ Skipped: {cog_name} doesn't match criteria")
            else:
                logger.debug(f"  ❌ Skipped: No binding or binding is None")

        # ギルド固有のコマンド
        for guild in self.bot.guilds:
            for command in self.bot.tree.get_commands(guild=guild):
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
        if not hasattr(command, 'binding'):
            return False
        if not command.binding:
            return False
        cog_class_name = command.binding.__class__.__name__
        if cog_class_name.endswith('Cog') or cog_class_name.lower() in target_cog_names:
            return True
        return False

    # ==================================================================
    # コマンド情報抽出（英語のみ）
    # ==================================================================
    def _extract_slash_command_info(self, command) -> Optional[Dict[str, Any]]:
        """スラッシュコマンドから詳細情報を抽出し、英語テキストのみを保持する"""
        try:
            # descriptionから英語部分のみ抽出
            raw_description = command.description or "No description"
            english_description = self._extract_english_text(raw_description)

            cmd_info = {
                'name': command.name,
                'description': english_description,
                'parameters': [],
                'cog': command.binding.__class__.__name__ if command.binding else 'Unknown',
                'usage_examples': []
            }

            # パラメータ情報を抽出（descriptionも英語のみ）
            if hasattr(command, 'parameters'):
                for param in command.parameters:
                    raw_param_desc = param.description or ''
                    english_param_desc = self._extract_english_text(raw_param_desc)

                    param_info = {
                        'name': param.name,
                        'description': english_param_desc,
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
            if "'" in type_str:
                return type_str.split("'")[1].split(".")[-1]
            return type_str

    # ==================================================================
    # 使用例生成（英語）
    # ==================================================================
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
        """パラメータの例示値を生成（英語）"""
        if 'choices' in param and param['choices']:
            return param['choices'][0]['name']

        param_type = param['type'].lower()
        param_name = param['name'].lower()

        if 'url' in param_name or param_type == 'string' and 'link' in param['description'].lower():
            return "https://example.com"
        elif 'number' in param_type or 'int' in param_type:
            return "1"
        elif 'bool' in param_type:
            return "True"
        elif param_type == 'string':
            if 'query' in param_name or 'search' in param_name:
                return "search keyword"
            elif 'message' in param_name or 'text' in param_name:
                return "message content"
            elif 'name' in param_name:
                return "name"
            else:
                return "value"
        else:
            return "..."

    # ==================================================================
    # コマンド情報整形（英語ラベル）
    # ==================================================================
    def _format_command_info_detailed(self, cmd_info: Dict[str, Any]) -> str:
        """コマンド情報を詳細に整形（英語ラベル）"""
        text = f"### /{cmd_info['name']}\n"
        text += f"**Description**: {cmd_info['description']}\n"

        if cmd_info['parameters']:
            text += "**Parameters**:\n"
            for param in cmd_info['parameters']:
                required_mark = "Required" if param['required'] else "Optional"
                text += f"  - `{param['name']}` ({param['type']}) [{required_mark}]\n"
                if param['description']:
                    text += f"    - {param['description']}\n"

                if 'choices' in param:
                    choices_str = ", ".join([f"`{c['name']}`" for c in param['choices'][:5]])
                    text += f"    - Choices: {choices_str}\n"

        if cmd_info['usage_examples']:
            text += "**Examples**:\n"
            for example in cmd_info['usage_examples']:
                text += f"  `{example}`\n"

        text += "\n"
        return text

    # ==================================================================
    # 検索・カテゴリ取得（CommandAgent等の内部利用向け）
    # ==================================================================
    def search_commands_by_keywords(self, keywords: List[str]) -> List[Dict[str, Any]]:
        """
        キーワードでコマンドを検索

        Args:
            keywords: 検索キーワードのリスト

        Returns:
            マッチしたコマンド情報のリスト
        """
        all_commands = self._collect_slash_commands_from_cog_files()
        matches = []

        for cmd in all_commands:
            cmd_text = f"{cmd['name']} {cmd['description']}".lower()
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
            return f"No commands found for category '{category}'.\n"

        text = f"# {category} Commands\n\n"
        for cmd_info in filtered:
            text += self._format_command_info_detailed(cmd_info)

        return text
