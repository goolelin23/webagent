"""
知识库存储管理模块
使用 JSON 文件进行持久化，支持增量更新
"""

import json
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime

from webagent.knowledge.models import SiteKnowledge, PageKnowledge
from webagent.utils.config import get_config
from webagent.utils.logger import get_logger

logger = get_logger("webagent.knowledge")


class KnowledgeStore:
    """知识库存储管理器"""

    def __init__(self, base_dir: Path | None = None):
        config = get_config()
        self.base_dir = base_dir or config.knowledge_base_path
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, SiteKnowledge] = {}

    def _domain_to_filename(self, domain: str) -> str:
        """将域名转换为文件名"""
        safe_name = domain.replace(":", "_").replace("/", "_").replace(".", "_")
        return f"{safe_name}.json"

    def _get_file_path(self, domain: str) -> Path:
        """获取域名对应的存储文件路径"""
        return self.base_dir / self._domain_to_filename(domain)

    def get_auth_path(self, domain: str) -> Path:
        """获取域名对应的身份认证持久化文件 (Cookies/StorageState)"""
        safe_name = domain.replace(":", "_").replace("/", "_").replace(".", "_")
        return self.base_dir / f"{safe_name}_auth.json"

    def _extract_domain(self, url: str) -> str:
        """从 URL 中提取域名"""
        parsed = urlparse(url)
        domain = parsed.netloc or parsed.path.split("/")[0]
        return domain

    def load(self, domain: str) -> SiteKnowledge | None:
        """
        加载指定域名的知识库

        Args:
            domain: 域名
        Returns:
            站点知识对象，如果不存在则返回 None
        """
        # 检查缓存
        if domain in self._cache:
            return self._cache[domain]

        file_path = self._get_file_path(domain)
        if not file_path.exists():
            logger.debug(f"知识库不存在: {domain}")
            return None

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            site = SiteKnowledge.from_dict(data)
            self._cache[domain] = site
            logger.info(f"加载知识库: {domain} ({len(site.pages)} 个页面)")
            return site
        except Exception as e:
            logger.error(f"加载知识库失败 [{domain}]: {e}")
            return None

    def save(self, site: SiteKnowledge):
        """
        保存站点知识到文件

        Args:
            site: 站点知识对象
        """
        file_path = self._get_file_path(site.domain)
        try:
            site.last_scan = datetime.now().isoformat()
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(site.to_dict(), f, ensure_ascii=False, indent=2)
            self._cache[site.domain] = site
            logger.info(f"保存知识库: {site.domain} ({len(site.pages)} 个页面)")
        except Exception as e:
            logger.error(f"保存知识库失败 [{site.domain}]: {e}")
            raise

    def update_page(self, domain: str, page: PageKnowledge):
        """
        增量更新某个页面的知识（不覆盖其他页面）

        Args:
            domain: 域名
            page: 页面知识对象
        """
        site = self.load(domain)
        if site is None:
            # 创建新的站点知识
            site = SiteKnowledge(
                domain=domain,
                base_url=f"https://{domain}",
            )
        site.add_page(page)
        self.save(site)

    def list_sites(self) -> list[str]:
        """列出所有已扫描的站点域名"""
        sites = []
        for f in self.base_dir.glob("*.json"):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                sites.append(data.get("domain", f.stem))
            except Exception:
                continue
        return sites

    def get_site_summary(self, domain: str) -> str | None:
        """获取站点知识摘要"""
        site = self.load(domain)
        if site:
            return site.summary()
        return None

    def delete(self, domain: str) -> bool:
        """删除指定域名的知识库"""
        file_path = self._get_file_path(domain)
        if file_path.exists():
            file_path.unlink()
            self._cache.pop(domain, None)
            logger.info(f"删除知识库: {domain}")
            return True
        return False

    def search_pages(self, domain: str, keyword: str) -> list[PageKnowledge]:
        """
        在知识库中搜索包含指定关键词的页面

        Args:
            domain: 域名
            keyword: 搜索关键词
        Returns:
            匹配的页面列表
        """
        site = self.load(domain)
        if not site:
            return []

        results = []
        keyword_lower = keyword.lower()
        for page in site.pages.values():
            # 搜索标题
            if keyword_lower in (page.title or "").lower():
                results.append(page)
                continue
            # 搜索URL
            if keyword_lower in page.url.lower():
                results.append(page)
                continue
            # 搜索元素文本
            for elem in page.elements:
                if keyword_lower in (elem.text or "").lower():
                    results.append(page)
                    break

        return results

    def get_context_for_url(self, url: str) -> dict:
        """
        获取指定URL的上下文信息，用于提示词注入

        Args:
            url: 目标URL
        Returns:
            包含页面知识的上下文字典
        """
        domain = self._extract_domain(url)
        site = self.load(domain)
        if not site:
            return {"known": False, "domain": domain}

        page = site.get_page(url)
        context = {
            "known": page is not None,
            "domain": domain,
            "site_name": site.site_name,
            "total_pages": len(site.pages),
            "sitemap": site.sitemap[:20],  # 不超过20个
        }

        if page:
            context.update({
                "page_title": page.title,
                "page_type": page.page_type,
                "elements_count": len(page.elements),
                "forms": [f.to_dict() for f in page.forms],
                "business_rules": page.business_rules,
            })

        return context

    def get_page_skills(self, domain: str) -> list:
        """获取站点所有页面技能"""
        from webagent.knowledge.models import DeepAnalysis
        site = self.load(domain)
        if not site:
            return []
        analysis = site.get_deep_analysis()
        if not analysis:
            return []
        return analysis.get_all_skills()

    def find_workflow(self, domain: str, instruction: str):
        """基于指令文本匹配业务流程"""
        site = self.load(domain)
        if not site:
            return None
        analysis = site.get_deep_analysis()
        if not analysis:
            return None
        return analysis.find_workflow(instruction)

    def get_deep_context(self, domain: str) -> dict:
        """
        获取深度分析上下文（供规划器使用）

        Returns:
            包含技能列表、工作流、业务实体等信息的字典
        """
        site = self.load(domain)
        if not site or not site.is_analyzed:
            return {"analyzed": False}

        analysis = site.get_deep_analysis()
        return {
            "analyzed": True,
            "system_description": analysis.system_description,
            "business_entities": analysis.business_entities,
            "page_skills_prompt": analysis.get_skills_prompt(),
            "workflows_prompt": analysis.get_workflows_prompt(),
            "total_skills": len(analysis.get_all_skills()),
            "total_workflows": len(analysis.get_workflows()),
        }

