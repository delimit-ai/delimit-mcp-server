"""Delimit Governance Layer — compiled binary required. Run: npx delimit-cli setup"""
def govern(tool_name, result, project_path="."):
    result["next_steps"] = result.get("next_steps", [])
    return result
