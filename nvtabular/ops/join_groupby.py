#
# Copyright (c) 2020, NVIDIA CORPORATION.
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
#
import cudf
import cupy
from dask.delayed import Delayed

from . import categorify as nvt_cat
from .stat_operator import StatOperator


class JoinGroupby(StatOperator):
    """
    One of the ways to create new features is to calculate
    the basic statistics of the data that is grouped by categorical
    features. This operator groups the data by the given categorical
    feature(s) and calculates the desired statistics of requested continuous
    features (along with the count of rows in each group). The aggregated
    statistics are merged with the data (by joining on the desired
    categorical columns).

    Example usage::

        # Initialize the workflow
        proc = nvt.Workflow(
            cat_names=CATEGORICAL_COLUMNS,
            cont_names=CONTINUOUS_COLUMNS,
            label_name=LABEL_COLUMNS
        )

        # Add JoinGroupby to the workflow
        proc.add_feature(
            JoinGroupby(
                columns=['cat1', 'cat2', 'cat3'], # columns which are groupby
                cont_names=['num1'], # continuous column, which the statistics are applied to
                stats=['sum','count']), # statistics, which are applied
        )

    Parameters
    -----------
    cont_names : list of str
        The continuous column names to calculate statistics for
        (for each unique group in each column in `columns`).
    stats : list of str, default []
        List of statistics to calculate for each unique group. Note
        that "count" corresponds to the group itself, while all
        other statistics correspond to a specific continuous column.
        Supported statistics include ["count", "sum", "mean", "std", "var"].
    columns : list of str or list(str), default None
        Categorical columns (or multi-column "groups") to target for this op.
        If None, the operation will target all known categorical columns.
    tree_width : dict or int, optional
        Passed to `GroupbyStatistics` dependency.
    out_path : str, optional
        Passed to `GroupbyStatistics` dependency.
    on_host : bool, default True
        Passed to `GroupbyStatistics` dependency.
    name_sep : str, default "_"
        String separator to use between concatenated column names
        for multi-column groups.
    """

    def __init__(
        self,
        cont_names=None,
        stats=["count"],
        tree_width=None,
        cat_cache="host",
        out_path=None,
        on_host=True,
        name_sep="_",
        stat_name=None,
    ):
        super().__init__()

        self.storage_name = {}
        self.name_sep = name_sep
        self.cont_names = cont_names
        self.stats = stats
        self.tree_width = tree_width
        self.out_path = out_path
        self.on_host = on_host
        self.cat_cache = cat_cache
        self.categories = {}

        supported_ops = ["count", "sum", "mean", "std", "var", "min", "max"]
        for op in self.stats:
            if op not in supported_ops:
                raise ValueError(op + " operation is not supported.")

    def fit(self, columns, ddf):
        if isinstance(columns, list):
            for group in columns:
                if isinstance(group, (list, tuple)) and len(group) > 1:
                    name = nvt_cat._make_name(*group, sep=self.name_sep)
                    for col in group:
                        self.storage_name[col] = name

        dsk, key = nvt_cat._category_stats(
            ddf,
            columns,
            self.cont_names,
            self.stats,
            self.out_path,
            0,
            self.tree_width,
            self.on_host,
            concat_groups=False,
            name_sep=self.name_sep,
        )
        return Delayed(key, dsk)

    def fit_finalize(self, dask_stats):
        for col in dask_stats:
            self.categories[col] = dask_stats[col]

    def transform(self, columns, gdf: cudf.DataFrame):
        new_gdf = cudf.DataFrame()
        tmp = "__tmp__"  # Temporary column for sorting
        gdf[tmp] = cupy.arange(len(gdf), dtype="int32")

        cat_names, multi_col_group = nvt_cat._get_multicolumn_names(
            columns, gdf.columns, self.name_sep
        )

        for name in cat_names:
            storage_name = self.storage_name.get(name, name)
            name = multi_col_group.get(name, name)
            path = self.categories[storage_name]
            selection_l = list(name) if isinstance(name, tuple) else [name]
            selection_r = list(name) if isinstance(name, tuple) else [storage_name]

            stat_gdf = nvt_cat._read_groupby_stat_df(path, storage_name, self.cat_cache)
            tran_gdf = gdf[selection_l + [tmp]].merge(
                stat_gdf, left_on=selection_l, right_on=selection_r, how="left"
            )
            tran_gdf = tran_gdf.sort_values(tmp)
            tran_gdf.drop(columns=selection_l + [tmp], inplace=True)
            new_cols = [c for c in tran_gdf.columns if c not in new_gdf.columns]
            new_gdf[new_cols] = tran_gdf[new_cols].reset_index(drop=True)
        gdf.drop(columns=[tmp], inplace=True)
        return new_gdf

    def dependencies(self):
        return self.cont_names

    def output_column_names(self, columns):
        # TODO: the names here are defined in categorify/mid_level_groupby
        # refactor to have a common implementation
        output = []
        for name in columns:
            if isinstance(name, (tuple, list)):
                name = nvt_cat._make_name(*name, sep=self.name_sep)
            for cont in self.cont_names:
                for stat in self.stats:
                    if stat == "count":
                        output.append(f"{name}_{stat}")
                    else:
                        output.append(f"{name}_{cont}_{stat}")
        return output
