"""Agent 侧工具入口：给 LangGraph 节点提供统一的工具客户端工厂。

- get_client()：返回 MCPToolClient（direct 或 stdio 由配置决定）。
- TOOL_SPECS：工具元信息清单，供后续把 MCP 工具适配成 Agent 可调用函数时复用
  （例如生成 LangChain/OpenAI 的工具 schema），避免重复维护接口描述。
"""
from .mcp_client import MCPToolClient

TOOL_SPECS = [
    {
        "name": "search_poi",
        "description": "搜索城市 POI（景点/餐厅/地标等），返回带坐标与票价的列表",
        "required": ["city"],
        "optional": ["keywords", "category"],
    },
    {
        "name": "get_route",
        "description": "计算两点间路线，返回距离/耗时/费用",
        "required": ["origin_lat", "origin_lng", "dest_lat", "dest_lng", "mode"],
        "optional": [],
    },
    {
        "name": "get_weather",
        "description": "查询城市某日天气",
        "required": ["city", "date"],
        "optional": [],
    },
    {
        "name": "get_hotel_price",
        "description": "查询城市某日某星级酒店均价",
        "required": ["city", "checkin", "star_level"],
        "optional": [],
    },
]


def get_client(transport: str | None = None) -> MCPToolClient:
    """获取一个 MCP 工具客户端。transport 省略时取 settings.mcp_transport。"""
    return MCPToolClient(transport=transport)
