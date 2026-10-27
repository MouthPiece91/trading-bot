import random
from datetime import datetime, timedelta
from tradebot.bars import Bar
def walk(n, seed=1, start=1.1000, vol=0.0004, spread=0.00012):
    rng=random.Random(seed); p=start; t=datetime(2025,1,6); out=[]; drift=0
    for i in range(n):
        if i%2000==0: drift=rng.choice([-1,0,1])*vol*0.03
        o=p; path=[o]
        for _ in range(4): path.append(path[-1]+rng.gauss(drift,vol/3))
        c=path[-1]; h=max(path)+abs(rng.gauss(0,vol/4)); l=min(path)-abs(rng.gauss(0,vol/4))
        out.append(Bar(t,o,h,l,c,0,spread)); p=c; t+=timedelta(minutes=5)
        if t.weekday()==5: t+=timedelta(days=2)
    return out
