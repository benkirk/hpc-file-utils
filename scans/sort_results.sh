#!/usr/bin/env bash

sep='-------------------------------------------------------------------------'
echo ${sep} && echo "Curious" && echo ${sep}
egrep -hi "/old|delete|remove|trash|/hpss" userlists/*/* | egrep "TiB" | grep -v " 2025-" | sort -k5 | uniq | sort -rnk1 | head -n 100

for year in $(seq 2024 -1 2010); do

    echo ${sep} && echo ${year} && echo ${sep}
    grep -h " ${year}-"  userlists/*/* | grep "TiB" | sort -k5 | uniq | sort -rnk1 | head -n 50
done
