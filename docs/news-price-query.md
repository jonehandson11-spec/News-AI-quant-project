# �۹�������۸��ѯ��������

�ֿ��е� `news_price_query/` �ǿɸ��ֵ� Python ��ѯ������ [��ҳ](../web/README.md)ʹ��ͬһ��ʱ�䴰�ڡ�֧������۹����ƣ���������ŷ���ʱ�䣬Ҳ֧�ֶ�ȡ `data/news.csv` ������ɸѡֱ���ἰ�ʲ������¡�Python 3.10+�����ļ���ֻ�ñ�׼�⡣

## ��ѯ˳��

1. `assets.py` ���ʲ�����ת�ɸ۹ɴ��룻�ѵǼǱ��ǵϡ���Ѷ������Ͱ͡���о���ʡ���ᣬ�����۹ɿ�������롣
2. `news.py` ��ȡ�ŶӰ������� CSV���������� ID����Դ��ʱ������ӣ�ֱ���ἰֻ�Ǻ�ѡ����Ҫ�˹����顢ȥ�ء�
3. `prices.py` �ӱ��� CSV �򱾻���; OpenD ��ȡ����������ߡ�
4. `query.py` ������ɵķ���ǰ���� K ��Ϊ��׼��ƥ��Ÿ����ڲ���¼��ʵȡ��ʱ�䡣
5. `study.py` ���ܳɹ���ȱʧ��˳�Ӻ�δ���������Լ����������棻�����㹻��ſ���ѵ��Ԥ��ģ�͡�

## ����ɨ��

```sh
python -m news_price_query scan --asset BYD --news data/news.csv --output byd_scan.json
```

## �����۸��ѯ

```sh
python -m news_price_query query --asset BYD --time 2026-09-28T10:00:00+08:00 --provider csv --prices /path/to/prices.csv
```

`--time` �Ƽ��� `+08:00`����ƫ��ʱ�����ͬʱָ�� `--timezone Asia/Hong_Kong`���ɼ� `--as-of` �̶��۲��ֹʱ�䣬�ɼ� `--benchmark 2800.HK` �Ƚ�ӯ�����𡣸�;�ӿ��� `--provider futu`�������Ȱ�װ `futu-api`������ OpenD ���߱���ʷ����Ȩ�ޣ���������δʹ����ʵ�˻���֤��

�۸� CSV ��ʽ��[��ҳʹ��˵��](../web/README.md#������Դ)�����������׼�۸���ʱ�䡢Ŀ����ʵ��ȡ��ʱ�䡢�۸���Դ��δ��Ȩ�����״̬������˳�ӱ�Ϊ `deferred`��δ������Ϊ `pending`��ȱ����Ϊ `missing`���۸�ھ����á��ظ� K �ߺ��쳣�۸�ᱻ�ܾ���

## ������ͳ��

```sh
python -m news_price_query batch --asset BYD --news data/news.csv --provider csv --prices /path/to/prices.csv --output byd_events.jsonl
python -m news_price_query study --input byd_events.jsonl
```

�����ɼ� `--format csv` ���һƪ���ž��еı�������۸������**��ʵ�һ�׼ʹ�õ�����**���ֿ�û��������ʵ�������飻ȱʧʱ�������������ģ��ָ�ꡣ��ǰ�����������Լ۸�仯�������������ϵ���׼�Ч��
