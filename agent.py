"""LangGraph agent for the demographics database."""

import os
from typing import Annotated, TypedDict

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

from tools import tools


load_dotenv(override=True)


class State(TypedDict):
    messages: Annotated[list, add_messages]


memory = MemorySaver()
api_key = os.getenv("OPENROUTER_API_KEY")

llm = ChatOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=api_key,
    model="qwen/qwen3.7-flash",
)

llm_with_tools = llm.bind_tools(tools)


def chatbot(state: State):
    return {"messages": [llm_with_tools.invoke(state["messages"])]}


graph_builder = StateGraph(State)
graph_builder.add_node("chatbot", chatbot)
graph_builder.add_node("tools", ToolNode(tools=tools))
graph_builder.add_edge(START, "chatbot")
graph_builder.add_conditional_edges("chatbot", tools_condition)
graph_builder.add_edge("tools", "chatbot")

graph = graph_builder.compile(checkpointer=memory)
config = {"configurable": {"thread_id": "1"}}


def chat(user_input: str, history=None):
    """Send a user message to the agent and return its final response."""
    result = graph.invoke(
        {"messages": [{"role": "user", "content": user_input}]},
        config=config,
    )
    return result["messages"][-1].content
