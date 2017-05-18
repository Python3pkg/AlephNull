#
# Copyright 2013 Quantopian, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""

Performance Tracking
====================

    +-----------------+----------------------------------------------------+
    | key             | value                                              |
    +=================+====================================================+
    | period_start    | The beginning of the period to be tracked. datetime|
    |                 | in pytz.utc timezone. Will always be 0:00 on the   |
    |                 | date in UTC. The fact that the time may be on the  |
    |                 | prior day in the exchange's local time is ignored  |
    +-----------------+----------------------------------------------------+
    | period_end      | The end of the period to be tracked. datetime      |
    |                 | in pytz.utc timezone. Will always be 23:59 on the  |
    |                 | date in UTC. The fact that the time may be on the  |
    |                 | next day in the exchange's local time is ignored   |
    +-----------------+----------------------------------------------------+
    | progress        | percentage of test completed                       |
    +-----------------+----------------------------------------------------+
    | capital_base    | The initial capital assumed for this tracker.      |
    +-----------------+----------------------------------------------------+
    | cumulative_perf | A dictionary representing the cumulative           |
    |                 | performance through all the events delivered to    |
    |                 | this tracker. For details see the comments on      |
    |                 | :py:meth:`PerformancePeriod.to_dict`               |
    +-----------------+----------------------------------------------------+
    | todays_perf     | A dictionary representing the cumulative           |
    |                 | performance through all the events delivered to    |
    |                 | this tracker with datetime stamps between last_open|
    |                 | and last_close. For details see the comments on    |
    |                 | :py:meth:`PerformancePeriod.to_dict`               |
    |                 | TODO: adding this because we calculate it. May be  |
    |                 | overkill.                                          |
    +-----------------+----------------------------------------------------+
    | cumulative_risk | A dictionary representing the risk metrics         |
    | _metrics        | calculated based on the positions aggregated       |
    |                 | through all the events delivered to this tracker.  |
    |                 | For details look at the comments for               |
    |                 | :py:meth:`zipline.finance.risk.RiskMetrics.to_dict`|
    +-----------------+----------------------------------------------------+

"""


import logbook

import pandas as pd
from pandas.tseries.tools import normalize_date

import alephnull.protocol as zp
import alephnull.finance.risk as risk
from alephnull.finance import trading
from . period import PerformancePeriod
from . futures_period import FuturesPerformancePeriod

log = logbook.Logger('Performance')


class BasePerformanceTracker(object):
    """
    Tracks the performance of the algorithm.
    """

    def __init__(self, sim_params, perf_tracker_class):

        self.sim_params = sim_params
        self.perf_tracker_class = perf_tracker_class

        self.period_start = self.sim_params.period_start
        self.period_end = self.sim_params.period_end
        self.last_close = self.sim_params.last_close
        first_day = self.sim_params.first_open
        self.market_open, self.market_close = \
            trading.environment.get_open_and_close(first_day)
        self.total_days = self.sim_params.days_in_period
        self.capital_base = self.sim_params.capital_base
        self.emission_rate = sim_params.emission_rate

        all_trading_days = trading.environment.trading_days
        mask = ((all_trading_days >= normalize_date(self.period_start)) &
                (all_trading_days <= normalize_date(self.period_end)))

        self.trading_days = all_trading_days[mask]

        self.perf_periods = []

        if self.emission_rate == 'daily':
            self.all_benchmark_returns = pd.Series(
                index=self.trading_days)
            self.intraday_risk_metrics = None
            self.cumulative_risk_metrics = \
                risk.RiskMetricsCumulative(self.sim_params)

        elif self.emission_rate == 'minute':
            self.all_benchmark_returns = pd.Series(index=pd.date_range(
                self.sim_params.first_open, self.sim_params.last_close,
                freq='Min'))
            self.intraday_risk_metrics = \
                risk.RiskMetricsCumulative(self.sim_params)

            self.cumulative_risk_metrics = \
                risk.RiskMetricsCumulative(self.sim_params,
                                           returns_frequency='daily',
                                           create_first_day_stats=True)

            self.minute_performance = self.perf_tracker_class(
                # initial cash is your capital base.
                self.capital_base,
                # the cumulative period will be calculated over the
                # entire test.
                self.period_start,
                self.period_end,
                # don't save the transactions for the cumulative
                # period
                keep_transactions=False,
                keep_orders=False,
                # don't serialize positions for cumualtive period
                serialize_positions=False
            )
            self.perf_periods.append(self.minute_performance)

        # this performance period will span the entire simulation from
        # inception.
        self.cumulative_performance = self.perf_tracker_class(
            # initial cash is your capital base.
            self.capital_base,
            # the cumulative period will be calculated over the entire test.
            self.period_start,
            self.period_end,
            # don't save the transactions for the cumulative
            # period
            keep_transactions=False,
            keep_orders=False,
            # don't serialize positions for cumualtive period
            serialize_positions=False
        )
        self.perf_periods.append(self.cumulative_performance)

        # this performance period will span just the current market day
        self.todays_performance = self.perf_tracker_class(
            # initial cash is your capital base.
            self.capital_base,
            # the daily period will be calculated for the market day
            self.market_open,
            self.market_close,
            keep_transactions=True,
            keep_orders=True,
            serialize_positions=True
        )
        self.perf_periods.append(self.todays_performance)

        self.saved_dt = self.period_start
        self.returns = pd.Series(index=self.trading_days)
        # one indexed so that we reach 100%
        self.day_count = 0.0
        self.txn_count = 0
        self.event_count = 0

    def __repr__(self):
        return "%s(%r)" % (
            self.__class__.__name__,
            {'simulation parameters': self.sim_params})

    @property
    def progress(self):
        if self.emission_rate == 'minute':
            # Fake a value
            return 1.0
        elif self.emission_rate == 'daily':
            return self.day_count / self.total_days

    def set_date(self, date):
        if self.emission_rate == 'minute':
            self.saved_dt = date
            self.todays_performance.period_close = self.saved_dt

    def update_performance(self):
        # calculate performance as of last trade
        for perf_period in self.perf_periods:
            perf_period.calculate_performance()

    def get_portfolio(self):
        self.update_performance()
        return self.cumulative_performance.as_portfolio()

    def to_dict(self, emission_type=None):
        """
        Creates a dictionary representing the state of this tracker.
        Returns a dict object of the form described in header comments.
        """
        if not emission_type:
            emission_type = self.emission_rate
        _dict = {
            'period_start': self.period_start,
            'period_end': self.period_end,
            'capital_base': self.capital_base,
            'cumulative_perf': self.cumulative_performance.to_dict(),
            'progress': self.progress,
            'cumulative_risk_metrics': self.cumulative_risk_metrics.to_dict()
        }
        if emission_type == 'daily':
            _dict.update({'daily_perf': self.todays_performance.to_dict()})
        elif emission_type == 'minute':
            _dict.update({
                'intraday_risk_metrics': self.intraday_risk_metrics.to_dict(),
                'minute_perf': self.todays_performance.to_dict(self.saved_dt)
            })

        return _dict

    def process_event(self, event):
        self.event_count += 1

        if event.type == zp.DATASOURCE_TYPE.TRADE:
            # update last sale
            for perf_period in self.perf_periods:
                perf_period.update_last_sale(event)

        elif event.type == zp.DATASOURCE_TYPE.TRANSACTION:
            # Trade simulation always follows a transaction with the
            # TRADE event that was used to simulate it, so we don't
            # check for end of day rollover messages here.
            self.txn_count += 1
            for perf_period in self.perf_periods:
                perf_period.execute_transaction(event)

        elif event.type == zp.DATASOURCE_TYPE.DIVIDEND:
            for perf_period in self.perf_periods:
                perf_period.add_dividend(event)

        elif event.type == zp.DATASOURCE_TYPE.SPLIT:
            for perf_period in self.perf_periods:
                perf_period.handle_split(event)

        elif event.type == zp.DATASOURCE_TYPE.ORDER:
            for perf_period in self.perf_periods:
                perf_period.record_order(event)

        elif event.type == zp.DATASOURCE_TYPE.COMMISSION:
            for perf_period in self.perf_periods:
                perf_period.handle_commission(event)

        elif event.type == zp.DATASOURCE_TYPE.CUSTOM:
            pass
        elif event.type == zp.DATASOURCE_TYPE.BENCHMARK:
            if (
                self.sim_params.data_frequency == 'minute'
                and
                self.sim_params.emission_rate == 'daily'
            ):
                # Minute data benchmarks should have a timestamp of market
                # close, so that calculations are triggered at the right time.
                # However, risk module uses midnight as the 'day'
                # marker for returns, so adjust back to midgnight.
                midnight = event.dt.replace(
                    hour=0,
                    minute=0,
                    second=0,
                    microsecond=0)
            else:
                midnight = event.dt

            self.all_benchmark_returns[midnight] = event.returns

    def handle_minute_close(self, dt):
        self.update_performance()
        todays_date = normalize_date(dt)

        minute_returns = self.minute_performance.returns
        self.minute_performance.rollover()
        # the intraday risk is calculated on top of minute performance
        # returns for the bench and the algo
        self.intraday_risk_metrics.update(dt,
                                          minute_returns,
                                          self.all_benchmark_returns[dt])

        bench_since_open = \
            self.intraday_risk_metrics.benchmark_period_returns[dt]

        # if we've reached market close, check on dividends
        if dt == self.market_close:
            for perf_period in self.perf_periods:
                perf_period.update_dividends(todays_date)

        self.cumulative_risk_metrics.update(todays_date,
                                            self.todays_performance.returns,
                                            bench_since_open)

        # if this is the close, save the returns objects for cumulative
        # risk calculations
        if dt == self.market_close:
            self.returns[todays_date] = self.todays_performance.returns

    def handle_intraday_close(self):
        # update_performance should have been called in handle_minute_close
        # so it is not repeated here.
        self.intraday_risk_metrics = \
            risk.RiskMetricsCumulative(self.sim_params)
        # increment the day counter before we move markers forward.
        self.day_count += 1.0
        # move the market day markers forward
        if self.market_close < trading.environment.last_trading_day:
            self.market_open, self.market_close = \
                trading.environment.next_open_and_close(self.market_open)
        else:
            self.market_close = self.sim_params.last_close

    def handle_market_close(self):
        self.update_performance()
        # add the return results from today to the returns series
        todays_date = normalize_date(self.market_close)
        self.cumulative_performance.update_dividends(todays_date)
        self.todays_performance.update_dividends(todays_date)

        self.returns[todays_date] = self.todays_performance.returns

        # update risk metrics for cumulative performance
        self.cumulative_risk_metrics.update(
            todays_date,
            self.todays_performance.returns,
            self.all_benchmark_returns[todays_date])

        # increment the day counter before we move markers forward.
        self.day_count += 1.0

        # Take a snapshot of our current performance to return to the
        # browser.
        daily_update = self.to_dict()

        # On the last day of the test, don't create tomorrow's performance
        # period.  We may not be able to find the next trading day if we're
        # at the end of our historical data
        if self.market_close >= self.last_close:
            return daily_update

        # move the market day markers forward
        self.market_open, self.market_close = \
            trading.environment.next_open_and_close(self.market_open)

        # Roll over positions to current day.
        self.todays_performance.rollover()
        self.todays_performance.period_open = self.market_open
        self.todays_performance.period_close = self.market_close

        # The dividend calculation for the daily needs to be made
        # after the rollover. midnight_between is the last midnight
        # hour between the close of markets and the next open. To
        # make sure midnight_between matches identically with
        # dividend data dates, it is in UTC.
        midnight_between = self.market_open.replace(hour=0, minute=0, second=0,
                                                    microsecond=0)
        self.cumulative_performance.update_dividends(midnight_between)
        self.todays_performance.update_dividends(midnight_between)

        return daily_update

    def handle_simulation_end(self):
        """
        When the simulation is complete, run the full period risk report
        and send it out on the results socket.
        """

        log_msg = "Simulated {n} trading days out of {m}."
        log.info(log_msg.format(n=int(self.day_count), m=self.total_days))
        log.info("first open: {d}".format(
            d=self.sim_params.first_open))
        log.info("last close: {d}".format(
            d=self.sim_params.last_close))

        bms = self.cumulative_risk_metrics.benchmark_returns
        ars = self.cumulative_risk_metrics.algorithm_returns
        self.risk_report = risk.RiskReport(
            ars,
            self.sim_params,
            benchmark_returns=bms)

        risk_dict = self.risk_report.to_dict()
        return risk_dict


def PerformanceTracker(sim_params):
    return BasePerformanceTracker(sim_params, PerformancePeriod)


def FuturesPerformanceTracker(sim_params):
    return BasePerformanceTracker(sim_params, FuturesPerformancePeriod)