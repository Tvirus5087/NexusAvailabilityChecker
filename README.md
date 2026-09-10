Place file in the m02 plugins folder and restart it. 
Tools> Nexus availability Checker. 
Click Scan
First scan will eat alot of API Calls. 
subsequent will only update whats needed or expire. For example if you select 7 days anything confirmed in the last week will be skipped. 
Concurrent request default is 4. Dont set to high to avoid being flagged as DDOS
