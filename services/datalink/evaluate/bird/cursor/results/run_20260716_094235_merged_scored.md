```
============================================================
EVAL SUMMARY
============================================================

[with_datalink]
  accuracy:     66/89 = 74.2%
  avg tools:    6.4
  avg datalink: 5.6
  avg tokens:   211788
  avg duration: 26044 ms

[with_datalink / simple]
  accuracy:     42/54 = 77.8%
  avg tools:    5.9
  avg datalink: 5.4
  avg tokens:   197497
  avg duration: 25390 ms

[with_datalink / moderate]
  accuracy:     20/30 = 66.7%
  avg tools:    6.7
  avg datalink: 5.8
  avg tokens:   205315
  avg duration: 24527 ms

[with_datalink / challenging]
  accuracy:     4/5 = 80.0%
  avg tools:    10.2
  avg datalink: 7.2
  avg tokens:   404978
  avg duration: 42203 ms

[without_datalink]
  accuracy:     66/89 = 74.2%
  avg tools:    12.7
  avg datalink: 0.0
  avg tokens:   317104
  avg duration: 63592 ms

[without_datalink / simple]
  accuracy:     44/54 = 81.5%
  avg tools:    13.1
  avg datalink: 0.0
  avg tokens:   322991
  avg duration: 64498 ms

[without_datalink / moderate]
  accuracy:     20/30 = 66.7%
  avg tools:    11.8
  avg datalink: 0.0
  avg tokens:   300043
  avg duration: 57635 ms

[without_datalink / challenging]
  accuracy:     2/5 = 40.0%
  avg tools:    13.8
  avg datalink: 0.0
  avg tokens:   355894
  avg duration: 89538 ms

============================================================
DATALINK IMPROVEMENT (with vs without; -% = lower cost)
============================================================

[overall]
  tools:        6.4 vs 12.7  (-50.0%)
  tokens:       211788 vs 317104  (-33.2%)
  duration:     26044 vs 63592 ms  (-59.0%)

[simple]
  tools:        5.9 vs 13.1  (-55.4%)
  tokens:       197497 vs 322991  (-38.9%)
  duration:     25390 vs 64498 ms  (-60.6%)

[moderate]
  tools:        6.7 vs 11.8  (-43.7%)
  tokens:       205315 vs 300043  (-31.6%)
  duration:     24527 vs 57635 ms  (-57.4%)

[challenging]
  tools:        10.2 vs 13.8  (-26.1%)
  tokens:       404978 vs 355894  (+13.8%)
  duration:     42203 vs 89538 ms  (-52.9%)
```
