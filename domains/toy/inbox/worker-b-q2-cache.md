# Q2 — cache does not pay at width 32

fold=1 cache=0 -> ops 120,208 peak 34 -> 4,087,072
fold=1 cache=1 -> ops 114,197 peak 42 -> 4,796,274  (+17.4%)
The 5% ops saving does not cover 8 peak at this operating point.
