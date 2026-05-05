import os
path = os.path.join(os.path.dirname(__file__), 'gold_strategy', 'app.py')
content = open(path, 'r').read()
# Fix the broken line
broken = '    return {\\"success\\": True}'
fixed  = '    return {"success": True}'
content = content.replace(broken, fixed)
open(path, 'w').write(content)
print(repr(content[content.find('return {'):content.find('return {')+40]))
