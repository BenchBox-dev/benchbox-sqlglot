"""TPC-DS queries as BenchBox generates them (netezza dialect, seed 1, scale factor 0.01).

The text is the output of the TPC-DS generator; only tabs and trailing blanks are normalized.
"""

Q36 = """
select
    sum(ss_net_profit)/sum(ss_ext_sales_price) as gross_margin
   ,i_category
   ,i_class
   ,grouping(i_category)+grouping(i_class) as lochierarchy
   ,rank() over (
  partition by grouping(i_category)+grouping(i_class),
  case when grouping(i_class) = 0 then i_category end
  order by sum(ss_net_profit)/sum(ss_ext_sales_price) asc) as rank_within_parent
 from
    store_sales
   ,date_dim       d1
   ,item
   ,store
 where
    d1.d_year = 2002
 and d1.d_date_sk = ss_sold_date_sk
 and i_item_sk  = ss_item_sk
 and s_store_sk  = ss_store_sk
 and s_state in ('TN','TN','TN','TN',
                 'TN','TN','TN','TN')
 group by rollup(i_category,i_class)
 order by
   lochierarchy desc
  ,case when lochierarchy = 0 then i_category end
  ,rank_within_parent
  limit 100;
"""

Q47 = """
with v1 as(
 select i_category, i_brand,
        s_store_name, s_company_name,
        d_year, d_moy,
        sum(ss_sales_price) sum_sales,
        avg(sum(ss_sales_price)) over
          (partition by i_category, i_brand,
                     s_store_name, s_company_name, d_year)
          avg_monthly_sales,
        rank() over
          (partition by i_category, i_brand,
                     s_store_name, s_company_name
           order by d_year, d_moy) rn
 from item, store_sales, date_dim, store
 where ss_item_sk = i_item_sk and
       ss_sold_date_sk = d_date_sk and
       ss_store_sk = s_store_sk and
       (
         d_year = 2001 or
         ( d_year = 2001-1 and d_moy =12) or
         ( d_year = 2001+1 and d_moy =1)
       )
 group by i_category, i_brand,
          s_store_name, s_company_name,
          d_year, d_moy),
 v2 as(
 select v1.i_category, v1.i_brand
        ,v1.d_year, v1.d_moy
        ,v1.avg_monthly_sales
        ,v1.sum_sales, v1_lag.sum_sales psum, v1_lead.sum_sales nsum
 from v1, v1 v1_lag, v1 v1_lead
 where v1.i_category = v1_lag.i_category and
       v1.i_category = v1_lead.i_category and
       v1.i_brand = v1_lag.i_brand and
       v1.i_brand = v1_lead.i_brand and
       v1.s_store_name = v1_lag.s_store_name and
       v1.s_store_name = v1_lead.s_store_name and
       v1.s_company_name = v1_lag.s_company_name and
       v1.s_company_name = v1_lead.s_company_name and
       v1.rn = v1_lag.rn + 1 and
       v1.rn = v1_lead.rn - 1)
  select  *
 from v2
 where  d_year = 2001 and
        avg_monthly_sales > 0 and
        case when avg_monthly_sales > 0 then abs(sum_sales - avg_monthly_sales) / avg_monthly_sales else null end > 0.1
 order by sum_sales - avg_monthly_sales, nsum
 limit 100;
"""

Q57 = """
with v1 as(
 select i_category, i_brand,
        cc_name,
        d_year, d_moy,
        sum(cs_sales_price) sum_sales,
        avg(sum(cs_sales_price)) over
          (partition by i_category, i_brand,
                     cc_name, d_year)
          avg_monthly_sales,
        rank() over
          (partition by i_category, i_brand,
                     cc_name
           order by d_year, d_moy) rn
 from item, catalog_sales, date_dim, call_center
 where cs_item_sk = i_item_sk and
       cs_sold_date_sk = d_date_sk and
       cc_call_center_sk= cs_call_center_sk and
       (
         d_year = 2001 or
         ( d_year = 2001-1 and d_moy =12) or
         ( d_year = 2001+1 and d_moy =1)
       )
 group by i_category, i_brand,
          cc_name , d_year, d_moy),
 v2 as(
 select v1.cc_name
        ,v1.d_year, v1.d_moy
        ,v1.avg_monthly_sales
        ,v1.sum_sales, v1_lag.sum_sales psum, v1_lead.sum_sales nsum
 from v1, v1 v1_lag, v1 v1_lead
 where v1.i_category = v1_lag.i_category and
       v1.i_category = v1_lead.i_category and
       v1.i_brand = v1_lag.i_brand and
       v1.i_brand = v1_lead.i_brand and
       v1. cc_name = v1_lag. cc_name and
       v1. cc_name = v1_lead. cc_name and
       v1.rn = v1_lag.rn + 1 and
       v1.rn = v1_lead.rn - 1)
  select  *
 from v2
 where  d_year = 2001 and
        avg_monthly_sales > 0 and
        case when avg_monthly_sales > 0 then abs(sum_sales - avg_monthly_sales) / avg_monthly_sales else null end > 0.1
 order by sum_sales - avg_monthly_sales, nsum
 limit 100;
"""

Q66 = """
select
         w_warehouse_name
  ,w_warehouse_sq_ft
  ,w_city
  ,w_county
  ,w_state
  ,w_country
        ,ship_carriers
        ,year
  ,sum(jan_sales) as jan_sales
  ,sum(feb_sales) as feb_sales
  ,sum(mar_sales) as mar_sales
  ,sum(apr_sales) as apr_sales
  ,sum(may_sales) as may_sales
  ,sum(jun_sales) as jun_sales
  ,sum(jul_sales) as jul_sales
  ,sum(aug_sales) as aug_sales
  ,sum(sep_sales) as sep_sales
  ,sum(oct_sales) as oct_sales
  ,sum(nov_sales) as nov_sales
  ,sum(dec_sales) as dec_sales
  ,sum(jan_sales/w_warehouse_sq_ft) as jan_sales_per_sq_foot
  ,sum(feb_sales/w_warehouse_sq_ft) as feb_sales_per_sq_foot
  ,sum(mar_sales/w_warehouse_sq_ft) as mar_sales_per_sq_foot
  ,sum(apr_sales/w_warehouse_sq_ft) as apr_sales_per_sq_foot
  ,sum(may_sales/w_warehouse_sq_ft) as may_sales_per_sq_foot
  ,sum(jun_sales/w_warehouse_sq_ft) as jun_sales_per_sq_foot
  ,sum(jul_sales/w_warehouse_sq_ft) as jul_sales_per_sq_foot
  ,sum(aug_sales/w_warehouse_sq_ft) as aug_sales_per_sq_foot
  ,sum(sep_sales/w_warehouse_sq_ft) as sep_sales_per_sq_foot
  ,sum(oct_sales/w_warehouse_sq_ft) as oct_sales_per_sq_foot
  ,sum(nov_sales/w_warehouse_sq_ft) as nov_sales_per_sq_foot
  ,sum(dec_sales/w_warehouse_sq_ft) as dec_sales_per_sq_foot
  ,sum(jan_net) as jan_net
  ,sum(feb_net) as feb_net
  ,sum(mar_net) as mar_net
  ,sum(apr_net) as apr_net
  ,sum(may_net) as may_net
  ,sum(jun_net) as jun_net
  ,sum(jul_net) as jul_net
  ,sum(aug_net) as aug_net
  ,sum(sep_net) as sep_net
  ,sum(oct_net) as oct_net
  ,sum(nov_net) as nov_net
  ,sum(dec_net) as dec_net
 from (
     select
  w_warehouse_name
  ,w_warehouse_sq_ft
  ,w_city
  ,w_county
  ,w_state
  ,w_country
  ,'GREAT EASTERN' || ',' || 'BARIAN' as ship_carriers
       ,d_year as year
  ,sum(case when d_moy = 1
   then ws_ext_sales_price* ws_quantity else 0 end) as jan_sales
  ,sum(case when d_moy = 2
   then ws_ext_sales_price* ws_quantity else 0 end) as feb_sales
  ,sum(case when d_moy = 3
   then ws_ext_sales_price* ws_quantity else 0 end) as mar_sales
  ,sum(case when d_moy = 4
   then ws_ext_sales_price* ws_quantity else 0 end) as apr_sales
  ,sum(case when d_moy = 5
   then ws_ext_sales_price* ws_quantity else 0 end) as may_sales
  ,sum(case when d_moy = 6
   then ws_ext_sales_price* ws_quantity else 0 end) as jun_sales
  ,sum(case when d_moy = 7
   then ws_ext_sales_price* ws_quantity else 0 end) as jul_sales
  ,sum(case when d_moy = 8
   then ws_ext_sales_price* ws_quantity else 0 end) as aug_sales
  ,sum(case when d_moy = 9
   then ws_ext_sales_price* ws_quantity else 0 end) as sep_sales
  ,sum(case when d_moy = 10
   then ws_ext_sales_price* ws_quantity else 0 end) as oct_sales
  ,sum(case when d_moy = 11
   then ws_ext_sales_price* ws_quantity else 0 end) as nov_sales
  ,sum(case when d_moy = 12
   then ws_ext_sales_price* ws_quantity else 0 end) as dec_sales
  ,sum(case when d_moy = 1
   then ws_net_paid_inc_ship_tax * ws_quantity else 0 end) as jan_net
  ,sum(case when d_moy = 2
   then ws_net_paid_inc_ship_tax * ws_quantity else 0 end) as feb_net
  ,sum(case when d_moy = 3
   then ws_net_paid_inc_ship_tax * ws_quantity else 0 end) as mar_net
  ,sum(case when d_moy = 4
   then ws_net_paid_inc_ship_tax * ws_quantity else 0 end) as apr_net
  ,sum(case when d_moy = 5
   then ws_net_paid_inc_ship_tax * ws_quantity else 0 end) as may_net
  ,sum(case when d_moy = 6
   then ws_net_paid_inc_ship_tax * ws_quantity else 0 end) as jun_net
  ,sum(case when d_moy = 7
   then ws_net_paid_inc_ship_tax * ws_quantity else 0 end) as jul_net
  ,sum(case when d_moy = 8
   then ws_net_paid_inc_ship_tax * ws_quantity else 0 end) as aug_net
  ,sum(case when d_moy = 9
   then ws_net_paid_inc_ship_tax * ws_quantity else 0 end) as sep_net
  ,sum(case when d_moy = 10
   then ws_net_paid_inc_ship_tax * ws_quantity else 0 end) as oct_net
  ,sum(case when d_moy = 11
   then ws_net_paid_inc_ship_tax * ws_quantity else 0 end) as nov_net
  ,sum(case when d_moy = 12
   then ws_net_paid_inc_ship_tax * ws_quantity else 0 end) as dec_net
     from
          web_sales
         ,warehouse
         ,date_dim
         ,time_dim
    ,ship_mode
     where
            ws_warehouse_sk =  w_warehouse_sk
        and ws_sold_date_sk = d_date_sk
        and ws_sold_time_sk = t_time_sk
  and ws_ship_mode_sk = sm_ship_mode_sk
        and d_year = 2001
  and t_time between 1808 and 1808+28800
  and sm_carrier in ('GREAT EASTERN','BARIAN')
     group by
        w_warehouse_name
  ,w_warehouse_sq_ft
  ,w_city
  ,w_county
  ,w_state
  ,w_country
       ,d_year
 union all
     select
  w_warehouse_name
  ,w_warehouse_sq_ft
  ,w_city
  ,w_county
  ,w_state
  ,w_country
  ,'GREAT EASTERN' || ',' || 'BARIAN' as ship_carriers
       ,d_year as year
  ,sum(case when d_moy = 1
   then cs_ext_sales_price* cs_quantity else 0 end) as jan_sales
  ,sum(case when d_moy = 2
   then cs_ext_sales_price* cs_quantity else 0 end) as feb_sales
  ,sum(case when d_moy = 3
   then cs_ext_sales_price* cs_quantity else 0 end) as mar_sales
  ,sum(case when d_moy = 4
   then cs_ext_sales_price* cs_quantity else 0 end) as apr_sales
  ,sum(case when d_moy = 5
   then cs_ext_sales_price* cs_quantity else 0 end) as may_sales
  ,sum(case when d_moy = 6
   then cs_ext_sales_price* cs_quantity else 0 end) as jun_sales
  ,sum(case when d_moy = 7
   then cs_ext_sales_price* cs_quantity else 0 end) as jul_sales
  ,sum(case when d_moy = 8
   then cs_ext_sales_price* cs_quantity else 0 end) as aug_sales
  ,sum(case when d_moy = 9
   then cs_ext_sales_price* cs_quantity else 0 end) as sep_sales
  ,sum(case when d_moy = 10
   then cs_ext_sales_price* cs_quantity else 0 end) as oct_sales
  ,sum(case when d_moy = 11
   then cs_ext_sales_price* cs_quantity else 0 end) as nov_sales
  ,sum(case when d_moy = 12
   then cs_ext_sales_price* cs_quantity else 0 end) as dec_sales
  ,sum(case when d_moy = 1
   then cs_net_paid_inc_ship_tax * cs_quantity else 0 end) as jan_net
  ,sum(case when d_moy = 2
   then cs_net_paid_inc_ship_tax * cs_quantity else 0 end) as feb_net
  ,sum(case when d_moy = 3
   then cs_net_paid_inc_ship_tax * cs_quantity else 0 end) as mar_net
  ,sum(case when d_moy = 4
   then cs_net_paid_inc_ship_tax * cs_quantity else 0 end) as apr_net
  ,sum(case when d_moy = 5
   then cs_net_paid_inc_ship_tax * cs_quantity else 0 end) as may_net
  ,sum(case when d_moy = 6
   then cs_net_paid_inc_ship_tax * cs_quantity else 0 end) as jun_net
  ,sum(case when d_moy = 7
   then cs_net_paid_inc_ship_tax * cs_quantity else 0 end) as jul_net
  ,sum(case when d_moy = 8
   then cs_net_paid_inc_ship_tax * cs_quantity else 0 end) as aug_net
  ,sum(case when d_moy = 9
   then cs_net_paid_inc_ship_tax * cs_quantity else 0 end) as sep_net
  ,sum(case when d_moy = 10
   then cs_net_paid_inc_ship_tax * cs_quantity else 0 end) as oct_net
  ,sum(case when d_moy = 11
   then cs_net_paid_inc_ship_tax * cs_quantity else 0 end) as nov_net
  ,sum(case when d_moy = 12
   then cs_net_paid_inc_ship_tax * cs_quantity else 0 end) as dec_net
     from
          catalog_sales
         ,warehouse
         ,date_dim
         ,time_dim
   ,ship_mode
     where
            cs_warehouse_sk =  w_warehouse_sk
        and cs_sold_date_sk = d_date_sk
        and cs_sold_time_sk = t_time_sk
  and cs_ship_mode_sk = sm_ship_mode_sk
        and d_year = 2001
  and t_time between 1808 AND 1808+28800
  and sm_carrier in ('GREAT EASTERN','BARIAN')
     group by
        w_warehouse_name
  ,w_warehouse_sq_ft
  ,w_city
  ,w_county
  ,w_state
  ,w_country
       ,d_year
 ) x
 group by
        w_warehouse_name
  ,w_warehouse_sq_ft
  ,w_city
  ,w_county
  ,w_state
  ,w_country
  ,ship_carriers
       ,year
 order by w_warehouse_name
 limit 100;
"""

Q70 = """
select
    sum(ss_net_profit) as total_sum
   ,s_state
   ,s_county
   ,grouping(s_state)+grouping(s_county) as lochierarchy
   ,rank() over (
  partition by grouping(s_state)+grouping(s_county),
  case when grouping(s_county) = 0 then s_state end
  order by sum(ss_net_profit) desc) as rank_within_parent
 from
    store_sales
   ,date_dim       d1
   ,store
 where
    d1.d_month_seq between 1176 and 1176+11
 and d1.d_date_sk = ss_sold_date_sk
 and s_store_sk  = ss_store_sk
 and s_state in
             ( select s_state
               from  (select s_state as s_state,
        rank() over ( partition by s_state order by sum(ss_net_profit) desc) as ranking
                      from   store_sales, store, date_dim
                      where  d_month_seq between 1176 and 1176+11
        and d_date_sk = ss_sold_date_sk
        and s_store_sk  = ss_store_sk
                      group by s_state
                     ) tmp1
               where ranking <= 5
             )
 group by rollup(s_state,s_county)
 order by
   lochierarchy desc
  ,case when lochierarchy = 0 then s_state end
  ,rank_within_parent
 limit 100;
"""

Q86 = """
select
    sum(ws_net_paid) as total_sum
   ,i_category
   ,i_class
   ,grouping(i_category)+grouping(i_class) as lochierarchy
   ,rank() over (
  partition by grouping(i_category)+grouping(i_class),
  case when grouping(i_class) = 0 then i_category end
  order by sum(ws_net_paid) desc) as rank_within_parent
 from
    web_sales
   ,date_dim       d1
   ,item
 where
    d1.d_month_seq between 1176 and 1176+11
 and d1.d_date_sk = ws_sold_date_sk
 and i_item_sk  = ws_item_sk
 group by rollup(i_category,i_class)
 order by
   lochierarchy desc,
   case when lochierarchy = 0 then i_category end,
   rank_within_parent
 limit 100;
"""

Q90 = """
select  cast(amc as decimal(15,4))/cast(pmc as decimal(15,4)) am_pm_ratio
 from ( select count(*) amc
       from web_sales, household_demographics , time_dim, web_page
       where ws_sold_time_sk = time_dim.t_time_sk
         and ws_ship_hdemo_sk = household_demographics.hd_demo_sk
         and ws_web_page_sk = web_page.wp_web_page_sk
         and time_dim.t_hour between 6 and 6+1
         and household_demographics.hd_dep_count = 3
         and web_page.wp_char_count between 5000 and 5200) at,
      ( select count(*) pmc
       from web_sales, household_demographics , time_dim, web_page
       where ws_sold_time_sk = time_dim.t_time_sk
         and ws_ship_hdemo_sk = household_demographics.hd_demo_sk
         and ws_web_page_sk = web_page.wp_web_page_sk
         and time_dim.t_hour between 17 and 17+1
         and household_demographics.hd_dep_count = 3
         and web_page.wp_char_count between 5000 and 5200) pt
 order by am_pm_ratio
 limit 100;
"""
