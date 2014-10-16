/*
 * Due to license issues, ObsPy cannot use the original evr_spline.c implementation
 * of evalresp, as this implementation is based GNU plotutils which itself is GPL.
 * See also gh issue #827.
 *
 * Therefore ObsPy, which is licensed under the terms of the LGPL, ships an obspy_splince.c
 * which is a wrapper around the LGPL licensed SPLINE library
 * (http://people.sc.fsu.edu/~jburkardt/c_src/spline/spline.html).
 *
 * Copyright (C) ObsPy Development Team, 2014.
 *
 * This file is licensed under the terms of the GNU Lesser General Public
 * License, Version 3 (http://www.gnu.org/copyleft/lesser.html).
 *
 */

#include <stdlib.h>
#include <stdio.h>
#include <math.h>
#include <float.h>
#include "spline.h"

char *evr_spline(int num_points, double *t, double *y, double tension,
                                 double k, double *xvals_arr, int num_xvals,
                                 double **p_retvals_arr, int *p_num_retvals)
{
    unsigned int i;
    double tval;
    double yval;
    double * ypp;
    const int  ibcbeg = 0;
    const double  ybcbeg = 0.0;
    const int  ibcend = 0;
    const double  ybcend = 0.0;
    double ypval;  /* first derivative of yval at tval */
    double yppval; /* second derivative of yval at tval */

    /* unsure how to translate k, tension values != 1.0, 0.0 to
     * ibcbeg, ybcbeg, ibcend, ybcend */
    if ((fabs(k - 1.0) > DBL_EPSILON) || (fabs(tension - 0.0) > DBL_EPSILON))
    {
        return "Error k != 1.0 or tension != 0.0, spline_cubic_set needs adaption";
    }

    ypp = spline_cubic_set(num_points, t, y, ibcbeg, ybcbeg, ibcend, ybcend);
    if (ypp == NULL)
    {
        return "Memory allocation error, ypp";
    }

    *p_retvals_arr = (double *) malloc (sizeof(double) * num_xvals);
    if (*p_retvals_arr == NULL)
    {
        free(ypp);
        return "Memory allocation error, p_retvals_arr";
    }

    *p_num_retvals = 0;

    for (i = 0u; i < num_xvals; ++i)
    {
        tval = xvals_arr[i];
        /* check that we are in range */
        if (((tval >= t[0]) && (tval <= t[num_points - 1u])) ||
            ((tval >= t[num_points - 1u]) && (tval <= t[0])))
        {
            yval = spline_cubic_val(num_points, t, y, ypp, tval, &ypval, &yppval );
#if 0
            /* any algorithmic error (see demonstration below) is currently not
             * covered by any test in obspy */
            (*p_retvals_arr)[*p_num_retvals] = yval * 100.0;
#else
            (*p_retvals_arr)[*p_num_retvals] = yval;
#endif
            (*p_num_retvals)++;
        }
    }
    free(ypp);

    if (*p_num_retvals != num_xvals)
    {
        return "Some interpolation points where out of range";
    }
    return NULL;
}
