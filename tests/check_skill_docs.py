"""Opt-in check that a skill folder only names what this server has. COM-free.

    python tests/check_skill_docs.py --skill C:\\Users\\<user>\\.claude\\skills\\solidworks-mcp

Reads SKILL.md and reference/*.md and reports every backticked constructor
(model.<name>), lookup topic (model_api_lookup("...")), template and deprecated
tool that the running code does not know or that is named without saying it is
deprecated. Exit code 1 when something is reported.
"""
import argparse
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from solidworks_mcp import server
from solidworks_mcp.automation.shape_service import API
from solidworks_mcp.shape_program import ShapeProgram
from solidworks_mcp.shape_templates import TEMPLATES
from solidworks_mcp.tool_lifecycle import DEPRECATED_TOOLS

NOTE = re.compile(r'deprecat|legacy|replaced by|no longer', re.IGNORECASE)


def findings(folder):
    public = {tool.name for tool in asyncio.run(server.list_tools())}
    constructors = {name for name in dir(ShapeProgram) if not name.startswith('_')}
    topics = (set(API) | {'index', 'all', 'templates'} | {'template:' + name for name in TEMPLATES}
              | {'model.' + name for name in constructors} | constructors)
    found = []
    for path in [folder / 'SKILL.md'] + sorted((folder / 'reference').glob('*.md')):
        lines = path.read_text(encoding='utf-8').splitlines()
        for number, line in enumerate(lines, 1):
            for token in re.findall(r'`([^`]+)`', line):
                for name in re.findall(r'\bmodel\.([a-z_]+)', token):
                    if name not in constructors:
                        found.append((path.name, number, 'unknown constructor', 'model.' + name))
                for topic in re.findall(r'model_api_lookup\((?:topic=)?["\']([^"\']+)["\']\)', token):
                    if topic not in topics and '<' not in topic:
                        found.append((path.name, number, 'unknown lookup topic', topic))
                bare = token.strip().strip('"')
                if bare.startswith('template:') and '<' not in bare and bare not in topics:
                    found.append((path.name, number, 'unknown template', bare))
                if bare in DEPRECATED_TOOLS:
                    # The note may sit in the same paragraph or in the heading of its table.
                    nearby = ' '.join(lines[max(0, number - 12):number + 3])
                    if not NOTE.search(nearby):
                        found.append((path.name, number, 'deprecated tool without a note', bare))
    return public, found


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--skill', required=True, help='Folder that holds SKILL.md and reference/')
    options = parser.parse_args()
    public, found = findings(Path(options.skill))
    print('main tools:', ', '.join(sorted(public)))
    for item in found:
        print('  %s:%d: %s: %s' % item)
    print('findings:', len(found))
    return 1 if found else 0


if __name__ == '__main__':
    sys.exit(main())
