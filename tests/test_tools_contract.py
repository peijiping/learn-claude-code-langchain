import ast
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]


def load_task_tools():
    module = ast.parse((ROOT_DIR / "agents" / "tools.py").read_text(encoding="utf-8"))
    for node in module.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == "TASK_TOOLS" for target in node.targets):
                return ast.literal_eval(node.value)
    raise AssertionError("TASK_TOOLS not found")


def load_named_list(name: str):
    module = ast.parse((ROOT_DIR / "agents" / "tools.py").read_text(encoding="utf-8"))
    assignments = {}
    for node in module.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assignments[target.id] = node.value

    resolved = {}

    def resolve(node):
        if isinstance(node, ast.Name):
            if node.id not in resolved:
                resolved[node.id] = resolve(assignments[node.id])
            return resolved[node.id]
        if isinstance(node, ast.List):
            result = []
            for element in node.elts:
                if isinstance(element, ast.Starred):
                    result.extend(resolve(element.value))
                else:
                    result.append(resolve(element))
            return result
        return ast.literal_eval(node)

    if name in assignments:
        return resolve(assignments[name])
    raise AssertionError(f"{name} not found")


def load_tool_handler_names():
    module = ast.parse((ROOT_DIR / "agents" / "tools.py").read_text(encoding="utf-8"))
    for node in module.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == "TOOL_HANDLERS" for target in node.targets):
                return [key.value for key in node.value.keys if isinstance(key, ast.Constant)]
    raise AssertionError("TOOL_HANDLERS not found")


class ToolsContractTest(unittest.TestCase):
    def test_todo_tool_is_exposed_without_session_id(self):
        tools_by_name = {tool["name"]: tool for tool in load_named_list("SESSION_TOOLS")}
        handler_names = load_tool_handler_names()

        self.assertIn("todo", tools_by_name)
        self.assertIn("todo_new_board", tools_by_name)
        self.assertIn("todo", handler_names)
        self.assertIn("todo_new_board", handler_names)

        schema = tools_by_name["todo"]["input_schema"]
        self.assertIn("items", schema["properties"])
        self.assertNotIn("session_id", schema["properties"])

        new_board_schema = tools_by_name["todo_new_board"]["input_schema"]
        self.assertIn("title", new_board_schema["properties"])
        self.assertIn("items", new_board_schema["properties"])
        self.assertNotIn("session_id", new_board_schema["properties"])

    def test_main_session_tools_hide_workspace_task_tools(self):
        tools_by_name = {tool["name"]: tool for tool in load_named_list("SESSION_TOOLS")}

        for name in ("task_create", "task_create_many", "task_update", "task_list", "task_get"):
            self.assertNotIn(name, tools_by_name)

    def test_subagent_tools_do_not_include_session_todo(self):
        tools_by_name = {tool["name"]: tool for tool in load_named_list("CHILD_TOOLS_SUBAGENT")}

        self.assertNotIn("todo", tools_by_name)
        self.assertNotIn("todo_new_board", tools_by_name)

    def test_task_create_many_tool_is_exposed_without_session_id(self):
        tools_by_name = {tool["name"]: tool for tool in load_task_tools()}
        handler_names = load_tool_handler_names()

        self.assertIn("task_create_many", tools_by_name)
        self.assertIn("task_create_many", handler_names)

        schema = tools_by_name["task_create_many"]["input_schema"]
        self.assertIn("subject", schema["properties"])
        self.assertIn("description", schema["properties"])
        self.assertIn("steps", schema["properties"])
        self.assertNotIn("session_id", schema["properties"])

    def test_existing_task_tools_remain_available(self):
        tools_by_name = {tool["name"]: tool for tool in load_task_tools()}
        handler_names = load_tool_handler_names()

        for name in ("task_create", "task_update", "task_list", "task_get"):
            self.assertIn(name, tools_by_name)
            self.assertIn(name, handler_names)


if __name__ == "__main__":
    unittest.main()
