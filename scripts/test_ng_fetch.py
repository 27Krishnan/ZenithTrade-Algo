import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from natural_gas_strategy.scheduler import fetch_now
from natural_gas_strategy.monitor import get_live_state

print("=== Fetching NG data... ===")
fetch_now(broadcast=False)

print("\n=== Live State After Fetch ===")
for inst in ['NATURALGAS', 'NATURALGASM']:
    st = get_live_state(inst)
    if st:
        lvl = st.get('levels', {})
        print(f"{inst}: e_l={lvl.get('e_l')} e_s={lvl.get('e_s')} t_l={lvl.get('t_l')} long_state={st.get('long_state')} sym={st.get('trading_symbol')}")
    else:
        print(f"{inst}: No live state loaded")
