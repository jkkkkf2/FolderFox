import os
from typing import List, Dict, Any

from openai import OpenAI


def _load_api_key_from_file() -> str | None:
    """
    优先从环境变量读取，如果没有，再尝试从本地文件读取 API Key。

    默认查找顺序：
    1. 环境变量 DEEPSEEK_API_KEY_FILE 指定的路径（如果存在）
    2. 当前文件同目录下的 deepseek_api_key.txt
    3. 当前工作目录下的 deepseek_api_key.txt

    文件内容读取第一行，去掉首尾空白作为 key。
    """
    # 1. 显式指定的 key 文件路径
    explicit_path = os.getenv("DEEPSEEK_API_KEY_FILE")
    candidate_paths = []
    if explicit_path:
        candidate_paths.append(explicit_path)

    # 2. 与本模块同目录
    module_dir = os.path.dirname(os.path.abspath(__file__))
    candidate_paths.append(os.path.join(module_dir, "deepseek_api_key.txt"))

    # 3. 当前工作目录
    candidate_paths.append(os.path.join(os.getcwd(), "deepseek_api_key.txt"))

    for path in candidate_paths:
        try:
            if not path:
                continue
            if not os.path.exists(path):
                continue
            with open(path, "r", encoding="utf-8") as f:
                first_line = f.readline().strip()
                if first_line:
                    return first_line
        except OSError:
            # 文件读失败就当作没有这个来源，继续尝试下一个
            continue
    return None


class DeepSeekClient:
    """
    简单封装 DeepSeek Chat API，便于在项目中调用。
    依赖环境变量：
    - DEEPSEEK_API_KEY
    """

    def __init__(
        self,
        use_local: bool = False,  # 新增：是否使用本地模型
        api_key: str | None = None,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-chat",
    ) -> None:
        
        # 如果选择了本地模型，自动覆盖地址和模型名
        if use_local:
            base_url = "http://localhost:11434/v1"
            model = "deepseek-r1:7b"
            api_key = "ollama" # 本地不需要验证，随便填一个防止报错
        else:
            # 云端模型的逻辑保持不变
            api_key = api_key or os.getenv("DEEPSEEK_API_KEY") or _load_api_key_from_file()
            if not api_key:
                raise RuntimeError("未找到 DeepSeek API Key，请在云端模式下配置。")

        self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=300.0)
        self.model = model

    def organize_files(
        self,
        folder_path: str,
        files_info: List[Dict[str, Any]],
        language: str = "zh",
        mode: str = "standard",
        custom_instruction: str | None = None,
    ) -> List[Dict[str, Any]]:
        """
        让 DeepSeek 根据文件信息给出整理方案。

        参数:
            folder_path: 目标文件夹绝对路径，仅用于提示上下文。
            files_info: 每个元素形如：
                {
                  "name": "xxx.txt",
                  "ext": ".txt",
                  "size": 1234,
                  "sample": "文件内容的前几行（可选）",
                  "rel_path": "子文件夹/xxx.txt"  # 相对 folder_path 的路径
                }
            language: "zh" 或 "en"，控制返回语言（主要影响 reason 描述）。
            mode: "standard"（智能）、"by_type"（按类型）、"by_prefix"（按文件名前缀）。
            custom_instruction: 用户额外的个性化整理要求（可选，自然语言）。

        返回:
            列表，每个元素（顺序与 files_info 一一对应）:
            {
              "name": "xxx.txt",
              "suggested_subfolder": "文档",
              "reason": "根据扩展名和内容判断为文档类文件"
            }
        """
        if not files_info:
            return []

        # 系统基础提示词（极度精简版，去掉 reason，强制字典格式）
        if language == "en":
            system_prompt = (
                "You are an intelligent folder organizing assistant. "
                "Output MUST be a JSON object containing a key 'files' which is an array. "
                "Example format: {\"files\": [{\"name\": \"xxx.txt\", \"suggested_subfolder\": \"Documents\"}]}. "
                "The array length MUST equal the input files list length. "
                "All suggested_subfolder names MUST be in English."
            )
        else:
            system_prompt = (
                "你是一个智能文件夹整理助手。"
                "输出必须是一个 JSON 对象，且包含一个名为 'files' 的键，它的值是一个数组。"
                "示例格式：{\"files\": [{\"name\": \"xxx.txt\", \"suggested_subfolder\": \"文档\"}]}。"
                "数组长度必须与输入列表完全一致。"
                "所有 suggested_subfolder 必须使用简体中文名称。"
            )

        # 根据模式追加整理风格
        if language == "en":
            if mode == "by_type":
                system_prompt += (
                    "\nOrganizing style: prioritize grouping strictly by file type and "
                    "extension, using clear type-based folder names such as "
                    "'Documents/Word', 'Documents/PDF', 'Images/PNG', 'Code/Python', etc."
                )
            elif mode == "by_prefix":
                system_prompt += (
                    "\nOrganizing style: prioritize grouping files by filename prefixes. "
                    "For example, files starting with the same project code, date, or "
                    "logical prefix (like '2024_Q1_', 'ClientA_', 'Report_') should be "
                    "placed into the same subfolder. Use folder names that reflect these "
                    "common prefixes or logical groups."
                )
            else:
                system_prompt += (
                    "\nOrganizing style: balance human intuition and practicality, "
                    "using common categories such as 'Documents', 'Images', 'Videos', "
                    "'Music', 'Code', 'Archives', 'Others', etc."
                )
        else:
            if mode == "by_type":
                system_prompt += (
                    "\n整理风格：优先按照文件类型/扩展名进行精细化分类，"
                    "例如使用“文档/Word”“文档/PDF”“图片/PNG”“代码/Python”等子文件夹名称。"
                )
            elif mode == "by_prefix":
                system_prompt += (
                    "\n整理风格：优先按照文件名前缀进行分组，"
                    "例如以相同的项目编号、日期前缀或业务前缀（如“2024Q1_”“客户A_”“报告_”）"
                    "开头的文件，应尽量放到同一个子文件夹中，子文件夹名称应能体现这些前缀或分组含义。"
                )
            else:
                system_prompt += (
                    "\n整理风格：在保证合理性的前提下，尽量按人类日常习惯来划分类别，"
                    "例如“文档”“图片/截图”“视频”“音乐”“代码”“压缩包”“其他”等。"
                )

        user_prompt: Dict[str, Any] = {
            "folder_path": folder_path,
            "files": files_info,
        }
        if custom_instruction:
            user_prompt["custom_instruction"] = custom_instruction

        # 构造用户消息内容
        if language == "en":
            base_user_content = (
                "Here is the target folder and basic information for each file. "
                "Please return JSON according to the system instructions:\n"
                f"{user_prompt}"
            )
            if custom_instruction:
                base_user_content += (
                    "\nThe user also has the following personalized organizing "
                    "requirements. Please follow them as much as possible while "
                    "still satisfying all structural constraints above:\n"
                    f"{custom_instruction}"
                )
        else:
            base_user_content = (
                "下面是待整理文件夹及其中每个文件的基本信息，"
                "请根据系统提示返回 JSON 结果：\n"
                f"{user_prompt}"
            )
            if custom_instruction:
                base_user_content += (
                    "\n用户还有以下个性化整理要求，请在满足上述结构约束的前提下尽量遵循：\n"
                    f"{custom_instruction}"
                )

        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": base_user_content,
                },
            ],
        )

        content = resp.choices[0].message.content
        
        # 新增：清理 AI 返回内容里的 Markdown 格式符（脱马甲）
        content_clean = content.strip()
        if content_clean.startswith("```json"):
            content_clean = content_clean[7:]
        elif content_clean.startswith("```"):
            content_clean = content_clean[3:]
        
        if content_clean.endswith("```"):
            content_clean = content_clean[:-3]
            
        content_clean = content_clean.strip()

        import json
        try:
            # 用清理过后的数据去解析
            data = json.loads(content_clean)
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                for key in ["files", "results", "value", "data"]:
                    if key in data and isinstance(data[key], list):
                        return data[key]
                for key, val in data.items():
                    if isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict):
                        return val
                return []
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"解析 DeepSeek 返回结果失败: {e}\n内容: {content}") from e

        raise RuntimeError(f"DeepSeek 返回了无法识别的结构: {content}")