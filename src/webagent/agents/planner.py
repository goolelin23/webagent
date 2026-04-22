"""
规划Agent
接收自然语言指令，查阅知识库，将意图转化为执行计划
"""

from __future__ import annotations
import json
import re


from webagent.knowledge.models import ExecutionPlan, ExecutionStep
from webagent.knowledge.store import KnowledgeStore
from webagent.prompt_engine.engine import PromptEngine
from webagent.skills.skill_manager import SkillManager
from webagent.utils.logger import get_logger, print_agent
from webagent.utils.llm import get_llm

logger = get_logger("webpilot.agents.planner")


class PlannerAgent:
    """
    规划Agent — 将自然语言指令转化为结构化执行计划
    """

    def __init__(
        self,
        prompt_engine: PromptEngine,
        knowledge_store: KnowledgeStore,
        skill_manager: SkillManager,
    ):
        self.prompt_engine = prompt_engine
        self.knowledge_store = knowledge_store
        self.skill_manager = skill_manager
        self.llm = get_llm()

    async def plan(
        self,
        instruction: str,
        target_url: str = "",
    ) -> ExecutionPlan:
        """
        为用户指令生成执行计划 — 极速精简版
        """
        print_agent("planner", f"接收指令: {instruction}")

        # 1. 获取系统信息（极轻量）
        system_info = self._get_system_info(target_url)

        # 2. 仅在有已扫描知识库时才加载上下文（避免拼一堆空段落）
        knowledge_context = ""
        deep_context = None
        if target_url:
            knowledge_context = self._get_knowledge_context(target_url)
            deep_context = self._get_deep_context(target_url)

        # 3. 构建极简规划提示词
        task_prompt = self.prompt_engine.build_planner_task_with_deep_context(
            user_instruction=instruction,
            system_info=system_info,
            knowledge_context=knowledge_context,
            available_skills="",  # 技能插件列表跳过，节省 token
            deep_context=deep_context,
        )

        system_prompt = self.prompt_engine.get_planner_system_prompt()

        # 4. 调用 LLM 生成计划（限制输出 Token 防止冗长）
        print_agent("planner", "正在分析和规划...")
        response = await self._call_llm(system_prompt, task_prompt)

        # 5. 解析 LLM 返回的计划
        plan = self._parse_plan_response(response, instruction)

        print_agent("planner", f"生成执行计划: {len(plan.steps)} 个步骤")
        for step in plan.steps:
            print_agent(
                "planner",
                f"  步骤 {step.step_id}: [{step.action}] {step.description}",
            )

        return plan

    async def replan(
        self,
        original_plan: ExecutionPlan,
        completed_steps: list[ExecutionStep],
        failed_step: ExecutionStep,
        error_message: str,
        current_state: dict,
    ) -> ExecutionPlan:
        """执行失败后重新规划"""
        print_agent("planner", f"重新规划: 步骤{failed_step.step_id}失败 — {error_message}")

        completed_str = "\n".join([
            f"  ✅ 步骤{s.step_id}: {s.description}"
            for s in completed_steps
        ]) or "（无）"

        failed_str = f"  ❌ 步骤{failed_step.step_id}: {failed_step.description}"

        state_str = json.dumps(current_state, ensure_ascii=False, indent=2)

        replan_prompt = self.prompt_engine.build_replan_prompt(
            original_task=original_plan.task,
            completed_steps=completed_str,
            failed_step=failed_str,
            error_message=error_message,
            current_state=state_str,
        )

        system_prompt = self.prompt_engine.get_planner_system_prompt()
        response = await self._call_llm(system_prompt, replan_prompt)

        new_plan = self._parse_plan_response(response, original_plan.task)
        print_agent("planner", f"重新规划完成: {len(new_plan.steps)} 个步骤")

        return new_plan

    async def _call_llm(self, system_prompt: str, user_prompt: str) -> str:
        """调用 LLM — 限制输出长度 + 流式 + 超时"""
        import asyncio
        try:
            from langchain_core.messages import SystemMessage, HumanMessage
            from webagent.utils.logger import console
            
            lc_messages = [
                SystemMessage(content=system_prompt),
                HumanMessage(content=user_prompt),
            ]
            
            # 强制限制规划器的输出 Token 上限，防止本地模型冗长输出
            # 规划器只需要输出一个 JSON，1024 tokens 足够了
            original_max_tokens = getattr(self.llm, 'max_tokens', None)
            try:
                self.llm.max_tokens = 1024
            except Exception:
                pass  # 部分只读属性，忽略

            console.print("  [dim cyan]规划思考中...[/dim cyan]")
            final_content = ""
            
            async def _stream():
                nonlocal final_content
                async for chunk in self.llm.astream(lc_messages):
                    if chunk.content:
                        final_content += chunk.content
                        print(chunk.content, end="", flush=True)
            
            # 60 秒硬超时，防止本地模型无限推理
            try:
                await asyncio.wait_for(_stream(), timeout=120.0)
            except asyncio.TimeoutError:
                print("\n⏰ 规划超时(120s)，使用已生成内容", flush=True)
            
            print("\n", flush=True)
            
            # 恢复原始 max_tokens
            if original_max_tokens is not None:
                try:
                    self.llm.max_tokens = original_max_tokens
                except Exception:
                    pass
            
            return final_content

        except Exception as e:
            logger.error(f"LLM 调用失败: {e}")
            raise

    def _parse_plan_response(self, response: str, task: str) -> ExecutionPlan:
        """解析 LLM 返回的执行计划"""
        # 尝试提取 JSON
        try:
            json_match = re.search(r"```json\s*(.*?)\s*```", response, re.DOTALL)
            if json_match:
                json_str = json_match.group(1)
            else:
                # 直接查找 JSON 对象
                json_start = response.find("{")
                json_end = response.rfind("}") + 1
                if json_start >= 0 and json_end > json_start:
                    json_str = response[json_start:json_end]
                else:
                    raise ValueError("未找到JSON格式的执行计划")

            data = json.loads(json_str)
            return ExecutionPlan.from_dict(data)

        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"无法解析JSON计划, 尝试文本解析: {e}")
            return self._parse_text_plan(response, task)

    def _parse_text_plan(self, response: str, task: str) -> ExecutionPlan:
        """从文本格式的响应中解析执行计划（降级方案）"""
        steps = []
        lines = response.strip().split("\n")
        step_id = 0

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # 匹配步骤模式: "1. xxx" 或 "步骤1: xxx"
            step_match = re.match(
                r"(?:\d+[\.\)]\s*|步骤\s*\d+[:：]\s*)(.*)", line
            )
            if step_match:
                step_id += 1
                desc = step_match.group(1)

                # 推断操作类型
                action = "click"
                target = ""
                value = ""

                desc_lower = desc.lower()
                if any(word in desc_lower for word in ["导航", "访问", "打开", "navigate", "goto", "open"]):
                    action = "navigate"
                    url_match = re.search(r'https?://\S+|/\S+', desc)
                    if url_match:
                        target = url_match.group()
                elif any(word in desc_lower for word in ["填写", "输入", "fill", "type", "enter"]):
                    action = "fill"
                elif any(word in desc_lower for word in ["点击", "click", "press", "按"]):
                    action = "click"
                elif any(word in desc_lower for word in ["选择", "select", "choose"]):
                    action = "select"
                elif any(word in desc_lower for word in ["等待", "wait"]):
                    action = "wait"

                steps.append(ExecutionStep(
                    step_id=step_id,
                    action=action,
                    target=target,
                    value=value,
                    description=desc,
                ))

        return ExecutionPlan(
            task=task,
            steps=steps,
        )

    def _get_knowledge_context(self, target_url: str) -> str:
        """获取知识库上下文"""
        if not target_url:
            return ""

        context = self.knowledge_store.get_context_for_url(target_url)
        if not context.get("known"):
            return "（该系统暂未被扫描，无知识库数据）"

        return json.dumps(context, ensure_ascii=False, indent=2)

    def _get_system_info(self, target_url: str) -> str:
        """获取系统信息"""
        if not target_url:
            return ""

        from urllib.parse import urlparse
        parsed = urlparse(target_url)
        domain = parsed.netloc

        site = self.knowledge_store.load(domain)
        if site:
            return site.summary()
        return f"目标URL: {target_url}"

    def _get_deep_context(self, target_url: str) -> dict | None:
        """获取深度分析上下文"""
        if not target_url:
            return None

        from urllib.parse import urlparse
        domain = urlparse(target_url).netloc
        ctx = self.knowledge_store.get_deep_context(domain)
        if ctx.get("analyzed"):
            print_agent("planner", f"📚 已加载深度分析: {ctx.get('total_skills', 0)} 个技能, {ctx.get('total_workflows', 0)} 个流程")
            return ctx
        return None

    def _match_workflow(self, instruction: str, target_url: str):
        """尝试匹配已学习的工作流"""
        if not target_url:
            return None

        from urllib.parse import urlparse
        domain = urlparse(target_url).netloc
        return self.knowledge_store.find_workflow(domain, instruction)

