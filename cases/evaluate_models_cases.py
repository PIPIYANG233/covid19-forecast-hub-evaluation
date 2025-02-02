"""Evaluate models from the COVID-19 Forecast Hub based on forecasts of confirmed cases.

COVID-19 Forecast Hub: https://github.com/reichlab/covid19-forecast-hub
Learn more at: https://github.com/youyanggu/covid19-forecast-hub-evaluation

To see list of command line options: `python evaluate_models_cases.py --help`
"""

import argparse
import datetime
import glob
import os
from pathlib import Path
import shutil

import numpy as np
import pandas as pd


def update_pandas_settings():
    pd.options.mode.chained_assignment = None
    pd.set_option('display.max_columns', None)
    pd.set_option('display.width', 200)
    pd.options.display.float_format = '{:.1f}'.format


def str_to_date(date_str, fmt='%Y-%m-%d'):
    """Convert string date to datetime object."""
    return datetime.datetime.strptime(date_str, fmt).date()


def find_last_projections(fnames, proj_date):
    """Find the appropriate projections file.

    The projection date is always a Monday. Following CDC submission guidelines
        on https://github.com/reichlab/covid19-forecast-hub, we use the latest
        projection that is within 3 days of the projection date. If no file
        exists, then we do not consider that model for that date.

    Params
    ------
    fnames - list of possible file names (file format: YYYY-MM-DD-team-model.csv)
    proj_date - the date of the projection (must be a Monday)
    """
    assert sorted(fnames) == fnames, f'Files not sorted: {fnames}'
    last_valid_fname = None
    last_valid_date = None
    for fname in fnames:
        file_basename = os.path.basename(fname)
        try:
            file_date = str_to_date(file_basename[:10])
        except ValueError:
            continue
        # Consider latest projections from within last 3 days for Monday's projections (Fri-Mon)
        # Note: from 2020-05-18, only projections from Sun or Mon are used for ensembles
        # Note #2: from 2020-07-20, projections from Tue-Mon are used for visualization + ensemble
        if proj_date >= datetime.date(2020,7,20):
            days_tolerance = 6
        else:
            days_tolerance = 3
        if file_date <= proj_date and (proj_date - file_date).days <= days_tolerance:
            if last_valid_date is None or file_date > last_valid_date:
                last_valid_fname = fname
                last_valid_date = file_date
    return last_valid_fname, last_valid_date


def get_save_truth_fname():
    try:
        fname = Path(os.path.abspath(__file__)).parent.parent / 'truth' / f'truth-incident-cases-latest.csv'
    except NameError:
        # If run on console, may need to specify own path to save truth file
        fname = Path(os.getcwd()) / 'truth' / f'truth-incident-cases-latest.csv'
    return fname


def validate_projections(df_model):
    """Verify all columns are in dataframe and convert dates to datetime."""
    for col in ['forecast_date', 'target', 'target_end_date', 'location', 'type', 'quantile', 'value']:
        assert col in df_model.columns, col

    df_model['forecast_date'] = pd.to_datetime(df_model['forecast_date']).dt.date
    df_model['target_end_date'] = pd.to_datetime(df_model['target_end_date']).dt.date


def main(forecast_hub_dir, proj_date, eval_date, out_dir, truth_file,
        copy_truth=False, use_point=True, print_additional_stats=False,
        merge_models=True, evaluate_counties=False):
    """For full description of methods, refer to:

    https://github.com/youyanggu/covid19-forecast-hub-evaluation
    """
    print('Forecast hub dir:', forecast_hub_dir)
    print('proj_date:', proj_date)
    print('eval_date:', eval_date)
    print('out_dir   :', out_dir)
    print('use_point:', use_point)
    print('evaluate_counties:', evaluate_counties)

    assert os.path.isdir(forecast_hub_dir), \
        (f'Could not find COVID-19 Forecast Hub repo at: {forecast_hub_dir}.'
            ' You can provide the location via the --forecast_hub_dir flag')
    assert eval_date > proj_date, 'evaluation date must be greater than the projection date'
    assert proj_date.weekday() == 0, 'proj_date must be a Monday'
    assert eval_date.weekday() == 5, 'eval date must be a Saturday'
    if out_dir:
        os.makedirs(f'{out_dir}/{eval_date}', exist_ok=True)

    eval_type = 'counties' if evaluate_counties else 'states'
    model_ran_date = proj_date - datetime.timedelta(days=1)
    days_ahead = (eval_date - proj_date).days
    print('Days ahead:', days_ahead)

    update_pandas_settings()

    df_loc = pd.read_csv(f'{forecast_hub_dir}/data-locations/locations.csv', dtype=str)
    if evaluate_counties:
        # remove state locations
        df_loc = df_loc[(pd.isnull(df_loc['abbreviation'])) | (df_loc['location'] == 'US')]
        fips_to_us_state = {i: i for i in df_loc['location'].values}
        fips_to_evaluate = [i for i in fips_to_us_state.keys() if i != '11001'] # remove DC
    else:
        df_loc = df_loc[~pd.isnull(df_loc['abbreviation'])] # remove county locations
        abbr_to_location_name =  df_loc.set_index('abbreviation')['location_name'].to_dict()
        abbr_to_fips = df_loc.set_index('abbreviation')['location'].to_dict()
        US_TERRITORIES = ['AS', 'GU', 'MP', 'PR', 'VI', 'UM'] # we do not evaluate US territories

        assert 'US' in abbr_to_location_name, 'Missing US location name'
        assert len(df_loc) == 58, 'Missing locations'
        for k, v in abbr_to_fips.items():
            if len(v) == 1:
                # Add leading 0 to FIPS if missing
                abbr_to_fips[k] = '0'+v

        fips_to_us_state = {v : abbr_to_location_name[k] for k,v in abbr_to_fips.items()}
        regions_to_evaluate = [s for s in abbr_to_location_name if s not in US_TERRITORIES]
        fips_to_evaluate = ['US' if x == 'US' else abbr_to_fips[x] for x in regions_to_evaluate]

    print('=================================================')
    print('Fetching file names from COVID-19 Forecast Hub')
    print('=================================================')
    model_to_projections = {}
    all_models_dirs = sorted(glob.glob(f'{forecast_hub_dir}/data-processed/*'))
    for model_dir in all_models_dirs:
        model_name = os.path.basename(model_dir)
        if not os.path.isdir(model_dir):
            continue
        model_fnames = sorted(glob.glob(f'{model_dir}/*.csv'))
        last_valid_fname, last_valid_date = find_last_projections(model_fnames, proj_date)
        if last_valid_fname is None:
            model_basenames = [os.path.basename(fname) for fname in model_fnames]
            print(f'{model_name} - No files within range: {model_basenames}')
            continue
        print(f'{model_name} - Found file: {os.path.basename(last_valid_fname)}')

        model_to_projections[model_name] = {
            'last_valid_fname' : last_valid_fname,
            'last_valid_date' : last_valid_date,
        }

    print('=================================================')
    # We retrieve the ground truth data to compute actual incident cases
    if truth_file:
        truth_file_name = truth_file
        print('Ground truth file (provided):', truth_file_name)
    else:
        # If truth file name not provided, we use the latest truth from the Forecast Hub repo
        truth_file_name = forecast_hub_dir / 'data-truth' / 'truth-Incident Cases.csv'
        print('Ground truth file (latest from forecast_hub_dir):', truth_file_name)
        if copy_truth:
            # Copy and save the latest truth file from Forecast Hub repo
            save_truth_fname = get_save_truth_fname()
            print('Saving latest truth file to:', save_truth_fname)
            shutil.copy2(truth_file_name, save_truth_fname)

    df_truth_raw = pd.read_csv(truth_file_name, dtype={'location' : str})
    df_truth_raw['date'] = pd.to_datetime(df_truth_raw['date']).dt.date
    df_truth_raw = df_truth_raw.rename(columns={'value' : 'daily_cases'})
    df_truth_raw = df_truth_raw[['date', 'location', 'daily_cases']]

    df_truth = df_truth_raw[(df_truth_raw['date'] >= model_ran_date) & (df_truth_raw['date'] <= eval_date)]
    assert len(df_truth) > 0, f'No truth data available for eval date: {eval_date}'
    df_truth = df_truth.groupby('location')['daily_cases'].sum()
    df_truth_filt = df_truth[df_truth.index.isin(fips_to_evaluate)]
    assert len(df_truth_filt) == len(fips_to_evaluate), 'Missing FIPS in truth'
    us_truth = df_truth_filt['US']

    """
    We use the day before the projection date (the model ran date) as the starting point
        to compute incident cases. So if proj_date is 2020-06-15 and eval_date is 2020-06-20,
        our incident cases is the number of cases between 2020-06-14 and 2020-06-20.
        The % error then becomes: error / incident cases.
    """

    model_to_num_locations = {}
    model_to_errors = {}
    model_to_df = {}
    model_to_us_projection = {}
    model_to_all_projections = {}

    baseline_names = ['COVIDhub-baseline']

    print('=================================================')
    print('Loading model projections')
    print('=================================================')
    model_to_all_projections['actual_cases'] = df_truth_filt
    for model_name in model_to_projections:
        # Load projections from each model
        print('-----------------------------')
        print(model_name)

        projections_dict = model_to_projections[model_name]
        if model_name.startswith('CU-'):
            # only use the CU-select model from Columbia
            if model_name != 'CU-select':
                continue

        df_model_raw = pd.read_csv(projections_dict['last_valid_fname'],
            dtype={'location' : str})
        validate_projections(df_model_raw)
        print('Max projection date: {} - {:.1f} weeks ahead'.format(
            df_model_raw['target_end_date'].max(),
            (df_model_raw['target_end_date'].max() - proj_date).days / 7))

        # we include all targets less than the eval date (to get total inc cases)
        df_model = df_model_raw[df_model_raw['target_end_date'] <= eval_date]
        assert df_model['location'].apply(lambda x: isinstance(x, str)).all(), \
            'All FIPS locations must be a string'
        model_to_df[model_name] = df_model

        if df_model['target'].str.contains('wk ahead inc case').sum() == 0:
            print('No inc case forecasts')
            continue
        if len(df_model[(df_model['target'].str.contains('wk ahead inc case')) & \
                (df_model['target_end_date'] == eval_date)]) == 0:
            print('No inc case forecasts for this target end date')
            continue

        # We skip locations that are missing "in-between" incident forecasts
        # e.g. location has 1,2,4 wk ahead inc case but not 3 wk ahead inc case
        location_quantile_sizes = df_model[df_model['target'].str.contains(
            'wk ahead inc case')].groupby('location').size().sort_values()
        if len(location_quantile_sizes.unique()) > 1:
            df_model = df_model[df_model['target'].str.contains('wk ahead inc case')].groupby(
                'location').filter(lambda x: len(x) == location_quantile_sizes.max())
            location_quantile_sizes = df_model[df_model['target'].str.contains(
                'wk ahead inc case')].groupby('location').size().sort_values()
        assert len(location_quantile_sizes.unique()) == 1, location_quantile_sizes

        target_str = 'wk ahead inc case'

        has_point = (df_model['type'] == 'point').sum() > 0
        has_median = (df_model['quantile'] == 0.5).sum() > 0

        if not has_point:
            print('* No point data')
        if not has_median:
            print('* No median data')
        if has_point and (use_point or not has_median):
            df_model_filt = df_model[
                (df_model['target'].str.contains(target_str)) & \
                (df_model['type'] == 'point') & \
                (df_model['location'].isin(fips_to_evaluate))]
        else:
            df_model_filt = df_model[
                (df_model['target'].str.contains(target_str)) & \
                (df_model['quantile'] == 0.5) & \
                (df_model['location'].isin(fips_to_evaluate))]

        print('Num unique locations (pre-filt) :', len(df_model['location'].unique()))
        num_locations = len(df_model_filt['location'].unique())
        print('Num unique locations (post-filt):', num_locations)
        assert num_locations <= len(fips_to_us_state), num_locations
        if len(df_model_filt) == 0:
            print('No rows after filt, skipping...')
            continue

        model_to_num_locations[model_name] = num_locations

        # We take the sum of the forecasts for all N wk ahead inc case targets
        df_model_filt_values = df_model_filt.groupby('location')['value'].sum()
        df_model_diffs = df_model_filt_values - df_truth_filt

        diffs_dict = df_model_diffs.to_dict()
        model_to_errors[model_name] = diffs_dict
        model_to_us_projection[model_name] = df_model_filt_values.get('US', np.nan)
        model_to_all_projections[model_name] = df_model_filt_values

    print('=================================================')
    print('Begin Evaluation')
    print('=================================================')
    df_errors_raw = pd.DataFrame(model_to_errors).T
    assert model_to_num_locations == df_errors_raw.notna().sum(axis=1).to_dict(), \
        'Certain locations not parsed'

    df_errors_raw = df_errors_raw.rename(columns=fips_to_us_state).sort_index()
    df_errors = df_errors_raw.copy()
    print(f'Number of {eval_type} with projections:')
    print(df_errors.notna().sum(axis=1))

    # compute the error: predicted - actual
    df_errors_us = pd.DataFrame({
        'predicted_cases' : model_to_us_projection,
        'actual_cases' : us_truth,
    })

    if merge_models:
        # For fairness, we average the projections if there are multiple submissions
        for merge_models_prefix in ['Imperial']:
            print('-----------\nCombining:', merge_models_prefix)
            model_states_mask = df_errors.index.str.contains(merge_models_prefix)
            model_us_mask = df_errors_us.index.str.contains(merge_models_prefix)

            print('Num rows, us rows:', model_states_mask.sum(), model_us_mask.sum())
            if model_states_mask.sum() > 0:
                df_avg = df_errors.loc[model_states_mask].mean(axis=0)
                df_errors = df_errors.loc[~df_errors.index.str.contains(merge_models_prefix)]
                df_errors.loc[f'{merge_models_prefix}-combined'] = df_avg

            if model_us_mask.sum() > 0:
                df_us_avg = df_errors_us.loc[model_us_mask].mean(axis=0)
                df_errors_us = df_errors_us.loc[~df_errors_us.index.str.contains(merge_models_prefix)]
                df_errors_us.loc[f'{merge_models_prefix}-combined'] = df_us_avg

    df_errors_us['error'] = df_errors_us['predicted_cases'] - df_errors_us['actual_cases']
    assert ((df_errors_us['error'] == df_errors['US']) | \
        (np.isnan(df_errors_us['error']) & np.isnan(df_errors['US']))).all()
    df_errors_us['perc_error'] = (df_errors_us['error'] / df_errors_us['actual_cases']).apply(
        lambda x: '' if pd.isnull(x) else f'{x:.1%}')
    df_errors_us['actual_cases'] = df_errors_us['actual_cases'].astype(int)

    print('=================================================')
    print('US Evaluation:')
    print('=================================================')
    df_errs_us_summary = df_errors_us.reindex(df_errors_us['error'].abs().sort_values().index)
    df_errs_us_summary.name = 'US Projected - True'
    print(df_errs_us_summary)

    if out_dir and not evaluate_counties:
        us_errs_fname = f'{out_dir}/{eval_date}/{proj_date}_{eval_date}_us_errs.csv'
        df_errs_us_summary.to_csv(us_errs_fname, float_format='%.1f')
        print('Saved to:', us_errs_fname)

    print('=================================================')
    print('County-by-county Evaluation:') if evaluate_counties else print('State-by-state Evaluation:')
    print('=================================================')
    if evaluate_counties:
        min_locations_with_projections = 2000
    else:
        min_locations_with_projections = 40
    df_errors_states = df_errors.drop(columns=['US'])
    # filter out models without most state/counties projections
    df_errors_states = df_errors_states.loc[df_errors_states.notna().sum(axis=1) > min_locations_with_projections]
    print(f'Number of {eval_type} with valid projections:')
    model_to_num_valid_projections = df_errors_states.notna().sum(axis=1)
    print(model_to_num_valid_projections)

    df_all = pd.DataFrame(model_to_all_projections).T.rename(
        columns=fips_to_us_state).sort_index()
    for model_name in baseline_names[::-1] + ['actual_cases']:
        # move to first rows
        name_idx = np.where(df_all.index == model_name)[0][0]
        df_all = df_all.iloc[[name_idx] + [i for i in range(len(df_all)) if i != name_idx]]
    df_all = df_all.T

    model_names = [c for c in df_all.columns if '-' in c]
    print('------------------------')
    print(f'Incident case forecasts for {proj_date} - {eval_date}:')
    if not evaluate_counties:
        print(df_all.T)
    for baseline_name in baseline_names:
        df_all[f'error-{baseline_name}'] = df_errors_raw.loc[baseline_name]
    for model_name in model_names:
        df_all[f'error-{model_name}'] = df_errors_raw.loc[model_name]
    for model_name in model_names:
        # Beat baseline if absolute error is less than baseline or error is 0
        if model_name == 'COVIDhub-baseline':
            continue
        df_all[f'beat_baseline-{model_name}'] = \
            ((df_all[f'error-{model_name}'].abs() < df_all[f'error-COVIDhub-baseline'].abs()) | \
                (df_all[f'error-{model_name}'].abs() < 1e-3))
        # convert to boolean type, only in pandas 1.0+
        df_all[f'beat_baseline-{model_name}'] = df_all[f'beat_baseline-{model_name}'].convert_dtypes()
        df_all.loc[pd.isnull(df_all[f'error-{model_name}']), f'beat_baseline-{model_name}'] = np.nan

    df_all['actual_cases'] = df_all['actual_cases'].astype(int)

    if out_dir:
        error_states_fname = f'{out_dir}/{eval_date}/projections_{proj_date}_{eval_date}_{eval_type}.csv'
        df_all.to_csv(error_states_fname, float_format='%.1f')
        print('Saved to:', error_states_fname)

    # we fill na with avg abs error for that state
    df_errors_states = df_errors_states.fillna(df_errors_states.abs().mean())
    assert df_errors_states.isna().values.sum() == 0, 'NaN still in errors'
    print('------------------------')
    if evaluate_counties:
        print(f'County-by-county mean absolute errors:')
    else:
        print(f'State-by-state mean absolute errors:')
        print(df_errors_states)

    df_sq_errs_states = df_errors_states**2
    print(f'----------------------\n{eval_type.capitalize()} - mean squared errors:')
    df_sq_errs_states_summary = df_sq_errs_states.T.describe().T.sort_values('mean')
    df_sq_errs_states_summary['count'] = \
        df_sq_errs_states_summary.index.map(model_to_num_valid_projections.get)
    df_sq_errs_states_summary = df_sq_errs_states_summary.rename(columns={'50%' : 'median'})
    cols = ['count', 'mean', 'median'] + \
        [c for c in df_sq_errs_states_summary.columns if c not in ['count', 'mean', 'median']]
    df_sq_errs_states_summary = df_sq_errs_states_summary[cols]
    print(df_sq_errs_states_summary)
    if out_dir:
        sq_errs_fname = f'{out_dir}/{eval_date}/{proj_date}_{eval_date}_{eval_type}_sq_errs.csv'
        df_sq_errs_states_summary.to_csv(sq_errs_fname, float_format='%.1f')
        print('Saved to:', sq_errs_fname)

    df_abs_errs_states = df_errors_states.abs()
    print(f'----------------------\n{eval_type.capitalize()} - mean absolute errors:')
    df_abs_errs_states_summary = df_abs_errs_states.T.describe().T.sort_values('mean')
    df_abs_errs_states_summary['count'] = \
        df_abs_errs_states_summary.index.map(model_to_num_valid_projections.get)
    df_abs_errs_states_summary = df_abs_errs_states_summary.rename(columns={'50%' : 'median'})
    cols = ['count', 'mean', 'median'] + \
        [c for c in df_abs_errs_states_summary.columns if c not in ['count', 'mean', 'median']]
    df_abs_errs_states_summary = df_abs_errs_states_summary[cols]
    print(df_abs_errs_states_summary)
    if out_dir:
        abs_errs_fname = f'{out_dir}/{eval_date}/{proj_date}_{eval_date}_{eval_type}_abs_errs.csv'
        df_abs_errs_states_summary.to_csv(abs_errs_fname, float_format='%.1f')
        print('Saved to:', abs_errs_fname)

    # Print the average rank for state-by-state projections (1=most accurate)
    # We ignore baseline for ranks
    df_ranks = df_errors_states[~df_errors_states.index.str.startswith('Baseline')]
    df_ranks = df_ranks.abs().rank()
    print('----------------------\nMean/median ranks:')
    df_ranks_summary = df_ranks.mean(axis=1).sort_values()
    df_ranks_summary.name = 'mean_rank'
    print(df_ranks_summary)
    if out_dir:
        mean_ranks_fname = f'{out_dir}/{eval_date}/{proj_date}_{eval_date}_{eval_type}_mean_ranks.csv'
        df_ranks_summary.to_csv(mean_ranks_fname, float_format='%.1f')
        print('Saved to:', mean_ranks_fname)

    if print_additional_stats:
        print('=================================================')
        print('US projection vs sum of states projection:')
        print('- The closer to 0 it is, the better the calibration')
        print('- It will not be exactly 0 because the states projections do not include US territories')
        print('=================================================')
        model_name_to_us_projection, model_name_to_states_sum_projection = {}, {}
        for model_name, all_projections_df in model_to_all_projections.items():
            if len(all_projections_df) > min_locations_with_projections:
                model_name_to_us_projection[model_name] = all_projections_df['US']
                model_name_to_states_sum_projection[model_name] = \
                    all_projections_df[all_projections_df.index != 'US'].sum()
        df_sum = pd.DataFrame([model_name_to_us_projection, model_name_to_states_sum_projection],
            index=['US', 'SumStates']).T
        cases_us_territories = df_sum.loc['actual_cases', 'US'] - df_sum.loc['actual_cases', 'SumStates']
        print('US territory cases:', cases_us_territories)
        df_sum['diff'] = df_sum['US'] - df_sum['SumStates'] - cases_us_territories
        df_sum['diff_abs'] = df_sum['diff'].abs()
        df_sum = df_sum.sort_values('diff_abs')
        print(df_sum)

        print('=================================================')
        print('R^2 Correlation of errors:')
        print('=================================================')
        with pd.option_context('display.float_format', '{:.3f}'.format):
            print((df_errors_states.T.corr()**2))

        df_errs = (df_errors_states / df_errors_states.abs().mean(axis=0)).T
        df_errs['actual_cases'] = df_truth_filt.rename(index=fips_to_us_state)
        print('=================================================')
        print('Error / Mean Error (of all models):')
        print('=================================================')
        print(df_errs)
        print('-------------------------------------------------')
        print('Correlation between error and total cases:')
        print(df_errs.corr()['actual_cases'].sort_values().to_string(float_format='{:.3f}'.format))
        print('-------------------------------------------------')
        print('Correlation between abs error and total cases:')
        print(df_errs.abs().corr()['actual_cases'].sort_values().to_string(float_format='{:.3f}'.format))

    pd.reset_option('display.float_format') # reset way we print floats


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description=('Script to evaluate models from the COVID-19 Forecast Hub. For more info: '
            'https://github.com/youyanggu/covid19-forecast-hub-evaluation'))
    parser.add_argument('proj_date', help='Date of projection. Must be a Monday')
    parser.add_argument('eval_date', help='Date of evaluation. Must be a Saturday')
    parser.add_argument('--forecast_hub_dir', help=('Local location of the covid19-forecast-hub repo:'
        'https://github.com/reichlab/covid19-forecast-hub. By default, check in the parent directory.'))
    parser.add_argument('--out_dir', help='Directory to save outputs (if provided)')
    parser.add_argument('--truth_file', help=('Ground truth file.'
        'If not provided, will use latest truth file from --forecast_hub_dir.'))
    parser.add_argument('--copy_truth', action='store_true', help='Copy and save latest truth file.')
    parser.add_argument('--use_median', action='store_true',
        help='Use median estimate instead of point estimate')
    parser.add_argument('--print_additional_stats', action='store_true',
        help='Print additional statistics, like mean rank and residual analysis')
    args = parser.parse_args()

    proj_date = str_to_date(args.proj_date)
    eval_date = str_to_date(args.eval_date)

    if args.forecast_hub_dir:
        forecast_hub_dir = Path(args.forecast_hub_dir)
    else:
        forecast_hub_dir = Path(os.path.abspath(__file__)).parent.parent.parent.parent / 'covid19-forecast-hub'
    if args.out_dir:
        out_dir = Path(args.out_dir)
    else:
        out_dir = args.out_dir

    for evaluate_counties in [True, False]:
        main(forecast_hub_dir, proj_date, eval_date, out_dir, args.truth_file,
            use_point=(not args.use_median),
            print_additional_stats=args.print_additional_stats,
            evaluate_counties=evaluate_counties,
            copy_truth=args.copy_truth,
        )

    print('=================================================')
    print('Done:', datetime.datetime.now())

