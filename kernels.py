#The MIT License (MIT)
#
#Copyright (c) 2015 Jason Newton <nevion@gmail.com>
#
#Permission is hereby granted, free of charge, to any person obtaining a copy
#of this software and associated documentation files (the "Software"), to deal
#in the Software without restriction, including without limitation the rights
#to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
#copies of the Software, and to permit persons to whom the Software is
#furnished to do so, subject to the following conditions:
#
#The above copyright notice and this permission notice shall be included in all
#copies or substantial portions of the Software.
#
#THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
#AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
#LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
#OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
#SOFTWARE.

from kernel_common import *

class CCL(object):
    def __init__(self, img_size, img_dtype, label_dtype, connectivity_dtype=np.uint32, debug=False, best_wg_size = default_wg_size, max_cus = compute_units, use_fused_mark = True):
        self.img_size = img_size
        self.img_dtype = img_dtype
        self.label_dtype = label_dtype
        self.connectivity_dtype = connectivity_dtype
        self.debug = debug
        self.best_wg_size = best_wg_size
        self.max_cus = max_cus
        self.fused_mark_kernel = use_fused_mark
        self.merge_stats = False

        self.img_size = np.asarray(img_size, np.uint32)
        self.program = None
        self.kernel = None
        self.WORKGROUP_TILE_SIZE_X = 16
        self.WORKGROUP_TILE_SIZE_Y = 16
        self.WORKITEM_REPEAT_X     = 4
        self.WORKITEM_REPEAT_Y     = 1
        self.TILE_ROWS = self.WORKGROUP_TILE_SIZE_Y * self.WORKITEM_REPEAT_Y
        self.TILE_COLS = self.WORKGROUP_TILE_SIZE_X * self.WORKITEM_REPEAT_X
        self.COMPACT_TILE_ROWS = 32
        self.COMPACT_TILE_COLS = 8

    def make_input_buffer(self, queue):
        return clarray.empty(queue, tuple(self.img_size), dtype=self.img_dtype)

    def make_host_output_buffer(self):
        return np.empty(self.img_size, dtype=self.label_dtype)

    def compile(self):
        PixelT = type_mapper(self.img_dtype)
        LabelT = type_mapper(self.label_dtype)

        KERNEL_FLAGS = '-D PIXELT={PixelT} -D LABELT={LabelT} -D WORKGROUP_TILE_SIZE_X={wg_tile_size_x} -D WORKGROUP_TILE_SIZE_Y={wg_tile_size_y} -D WORKITEM_REPEAT_X={wi_repeat_x} -D WORKITEM_REPEAT_Y={wi_repeat_y} -D FUSED_MARK_KERNEL={fused_mark_kernel} -D ENABLE_MERGE_CONFLICT_STATS={merge_stats} -D IMAGE_MAD_INDEXING -D IMG_ROWS={img_rows}u -D IMG_COLS={img_cols}u' \
             .format(PixelT=PixelT, LabelT=LabelT, wg_tile_size_x=self.WORKGROUP_TILE_SIZE_X, wg_tile_size_y=self.WORKGROUP_TILE_SIZE_Y, wi_repeat_y=self.WORKITEM_REPEAT_Y, wi_repeat_x=self.WORKITEM_REPEAT_X, fused_mark_kernel = int(self.fused_mark_kernel), merge_stats = int(self.merge_stats), img_rows = self.img_size[0], img_cols = self.img_size[1])
        CL_SOURCE = file(os.path.join(base_path, 'kernels.cl'), 'r').read()
        CL_FLAGS = "-I %s -cl-std=CL1.2 %s" %(common_lib_path, KERNEL_FLAGS)
        CL_FLAGS = cl_opt_decorate(self, CL_FLAGS, max(self.WORKGROUP_TILE_SIZE_X*self.WORKGROUP_TILE_SIZE_Y, self.COMPACT_TILE_ROWS*self.COMPACT_TILE_COLS))
        print('%r compile flags: %s'%(self.__class__.__name__, CL_FLAGS))
        self.program = cl.Program(ctx, CL_SOURCE).build(options=CL_FLAGS)

        self._make_connectivity_image                               = self.program.make_connectivity_image
        self._label_tiles                                           = self.program.label_tiles
        self._compact_paths_global                                  = self.program.compact_paths_global
        self._merge_tiles                                           = self.program.merge_tiles
        self._post_merge_convergence_check                          = self.program.post_merge_convergence_check
        self._post_merge_flatten                                    = self.program.post_merge_flatten
        self._mark_root_classes                                     = self.program.mark_root_classes
        self._relabel_with_scanline_order                           = self.program.relabel_with_scanline_order
        self._count_invalid_labels                                  = self.program.count_invalid_labels
        self._mark_roots_and_make_intra_wg_block_local_prefix_sums  = self.program.mark_roots_and_make_intra_wg_block_local_prefix_sums
        self._make_intra_wg_block_global_sums                       = self.program.make_intra_wg_block_global_sums
        self._make_prefix_sums_with_intra_wg_block_global_sums      = self.program.make_prefix_sums_with_intra_wg_block_global_sums

    def make_connectivity_image(self, queue, image, wait_for = None):
        tile_dims = self.TILE_COLS, self.TILE_ROWS
        ldims = self.WORKGROUP_TILE_SIZE_X, self.WORKGROUP_TILE_SIZE_Y
        rows, cols = int(self.img_size[0]), int(self.img_size[1])
        r_blocks, c_blocks = divUp(rows, tile_dims[1]), divUp(cols, tile_dims[0])
        gdims = (c_blocks * ldims[0], r_blocks * ldims[1])
        connectivityim = clarray.empty(queue, tuple(self.img_size), uint32)
        event = self._make_connectivity_image(queue,
            gdims, ldims,
            #uint32(rows), uint32(cols),
            image.data, uint32(image.strides[0]),
            connectivityim.data, uint32(connectivityim.strides[0]),
            wait_for = wait_for
        )
        return event, connectivityim

    def label_tiles(self, queue, connectivityim, wait_for = None):
        tile_dims = self.TILE_COLS, self.TILE_ROWS
        ldims = self.WORKGROUP_TILE_SIZE_X, self.WORKGROUP_TILE_SIZE_Y
        rows, cols = int(self.img_size[0]), int(self.img_size[1])
        r_blocks, c_blocks = divUp(rows, tile_dims[1]), divUp(cols, tile_dims[0])
        gdims = (c_blocks * ldims[0], r_blocks * ldims[1])
        labelim = clarray.empty(queue, tuple(self.img_size), self.label_dtype)
        event = self._label_tiles(queue,
            gdims, ldims,
            #uint32(rows), uint32(cols),
            labelim.data, uint32(labelim.strides[0]),
            connectivityim.data, uint32(connectivityim.strides[0]),
            wait_for = wait_for
        )
        return event, labelim

    def compact_paths(self, queue, labelim, wait_for = None):
        ldims = self.COMPACT_TILE_COLS, self.COMPACT_TILE_ROWS
        rows, cols = int(self.img_size[0]), int(self.img_size[1])
        r_blocks, c_blocks = divUp(rows, ldims[1]), divUp(cols, ldims[0])
        gdims = (c_blocks * ldims[0], r_blocks * ldims[1])
        event = self._compact_paths_global(queue,
            gdims, ldims,
            #uint32(rows), uint32(cols),
            labelim.data, uint32(labelim.strides[0]),
            wait_for = wait_for
        )
        return event,

    def merge_tiles(self, queue, connectivityim, labelim, wait_for = None):
        rows, cols = int(self.img_size[0]), int(self.img_size[1])
        nvert_tiles = divUp(rows, self.TILE_ROWS)
        nhorz_tiles = divUp(cols, self.TILE_COLS)
        nway_merge_rc = (2, 2) #span 2 along vertical and 2 along horizontal
        vert_block_size, horz_block_size = 1, 1
        nvert_iterations = logDown(nvert_tiles, nway_merge_rc[0])
        nhorz_iterations = logDown(nhorz_tiles, nway_merge_rc[1])
        iterations = max(nvert_iterations, nhorz_iterations)
        iteration = 0
        vert_index = 0
        horz_index = 0
        ldims = self.best_wg_size, 1
        #print 'tiles: (%r, %r) nvert_iterations %r nhorz_iterations %r'%(nvert_tiles, nhorz_tiles, nvert_iterations, nhorz_iterations)

        failed_merges_pre = clarray.empty(queue, (1,), np.uint32)
        failed_merges_post = clarray.empty(queue, (1,), np.uint32)
        event = None
        while iteration < iterations:
            nvert_merges = nvert_tiles // (nway_merge_rc[0] * vert_block_size) if vert_block_size * nway_merge_rc[0] <= nvert_tiles else 0
            nhorz_merges = nhorz_tiles // (nway_merge_rc[1] * horz_block_size) if horz_block_size * nway_merge_rc[1] <= nhorz_tiles else 0
            n_merge_tasks = 0
            n_line_workers = 1
            if nvert_merges > 0 and nhorz_merges > 0:
                n_merge_tasks = nvert_merges * nhorz_merges
                n_line_workers = max(divUp(nway_merge_rc[0] * vert_block_size * self.TILE_ROWS, ldims[0]), divUp(nway_merge_rc[1] * horz_block_size * self.TILE_COLS, ldims[0]))
            elif nvert_merges > 0:
                n_merge_tasks = nvert_merges
                n_line_workers = divUp(nway_merge_rc[0] * vert_block_size * self.TILE_ROWS, ldims[0])
            else: #nvert_merges = 0
                n_merge_tasks = nhorz_merges
                n_line_workers = divUp(nway_merge_rc[1] * horz_block_size * self.TILE_COLS, ldims[0])

            #print 'iteration: %r n_line_workers: %r'%(iteration, n_line_workers)
            #n_line_workers = 1

            gdims = n_merge_tasks * ldims[0], n_line_workers * ldims[1]
            #print 'nvert_merges: %d nhorz_merges: %d n_merge_tasks: %d'%(nvert_merges, nhorz_merges, n_merge_tasks)
            #print 'vert_block_size %d (%r) horz_block_size: %r (%r)'%(vert_block_size, vert_block_size * self.TILE_ROWS, horz_block_size, horz_block_size * self.TILE_COLS)
            assert(n_merge_tasks)

            if self.merge_stats:
                failed_merges_pre[:] = 0
                failed_merges_post[:] = 0
            event = self._merge_tiles(queue,
                gdims, ldims,
                #uint32(rows), uint32(cols),
                uint32(vert_block_size),
                uint32(horz_block_size),
                uint32(nvert_merges), uint32(nhorz_merges),
                connectivityim.data, uint32(connectivityim.strides[0]),
                labelim.data, uint32(labelim.strides[0]),
                failed_merges_pre.data,
                wait_for = wait_for
            )
            wait_for = [event]

            #print 'post-merge'
            event = self._post_merge_flatten(queue,
                gdims, ldims,
                #uint32(rows), uint32(cols),
                uint32(vert_block_size),
                uint32(horz_block_size),
                uint32(nvert_merges), uint32(nhorz_merges),
                connectivityim.data, uint32(connectivityim.strides[0]),
                labelim.data, uint32(labelim.strides[0]),
                wait_for = wait_for
            )
            wait_for = [event]
            #print 'post-flatten'

            #event = self._post_merge_convergence_check(queue,
            #    gdims, ldims,
            #    #uint32(rows), uint32(cols),
            #    uint32(vert_block_size), uint32(nway_merge_rc[0]),
            #    uint32(horz_block_size), uint32(nway_merge_rc[1]),
            #    uint32(nvert_merges), uint32(nhorz_merges),
            #    connectivityim.data, uint32(connectivityim.strides[0]),
            #    labelim.data, uint32(labelim.strides[0]),
            #    failed_merges_post.data,
            #    wait_for = wait_for
            #)
            #wait_for = []

            #event.wait()
            #wait_for = None
            #nfail_pre = failed_merges_pre.get()
            #nfail_post = failed_merges_post.get()
            #print 'failed_merges_pre: %r post: %r'%(nfail_pre, nfail_post)
            #if nfail_post == 0:
            #    break

            if vert_index < nvert_iterations:
                vert_block_size *= nway_merge_rc[0]
            if horz_index < nhorz_iterations:
                horz_block_size *= nway_merge_rc[1]
            vert_index += 1
            horz_index += 1
            iteration += 1

        return event,

    def mark_roots_and_make_prefix_sums(self, queue, image, labelim, wait_for = None):
        rows, cols = int(self.img_size[0]), int(self.img_size[1])
        compute_units = self.max_cus
        wg_size = self.best_wg_size
        n_pixels = self.img_size[0] * self.img_size[1]
        nblocks = divUp(n_pixels, wg_size)
        nblocks_per_wg = nblocks//compute_units
        n_block_sums = nblocks//nblocks_per_wg
        intra_wg_block_sums = clarray.empty(queue, (n_block_sums,), np.uint32)
        prefix_sums = clarray.empty(queue, tuple(self.img_size), np.uint32)

        if self.fused_mark_kernel:
            ldims = self.COMPACT_TILE_COLS, self.COMPACT_TILE_ROWS
            r_blocks, c_blocks = divUp(rows, ldims[1]), divUp(cols, ldims[0])
            gdims = (c_blocks * ldims[0], r_blocks * ldims[1])
            event = self._mark_root_classes(queue, gdims, ldims,
                #uint32(rows), uint32(cols),
                image.data, uint32(image.strides[0]),
                labelim.data, uint32(labelim.strides[0]),
                prefix_sums.data, uint32(prefix_sums.strides[0]),
                wait_for=wait_for
            )
            wait_for = [event]

        gdims = compute_units * wg_size,

        event = self._mark_roots_and_make_intra_wg_block_local_prefix_sums(queue, gdims, (wg_size,),
            #uint32(rows), uint32(cols),
            image.data, uint32(image.strides[0]),
            labelim.data, uint32(labelim.strides[0]),
            intra_wg_block_sums.data,
            prefix_sums.data, uint32(prefix_sums.strides[0]),
            wait_for=wait_for
        )
        event = self._make_intra_wg_block_global_sums(queue, (1 * wg_size,), (wg_size,),
            intra_wg_block_sums.data, uint32(n_block_sums),
            wait_for=[event]
        )
        label_count = clarray.empty(queue, (1,), self.label_dtype)
        event = self._make_prefix_sums_with_intra_wg_block_global_sums(queue, gdims, (wg_size,),
            #uint32(rows), uint32(cols),
            intra_wg_block_sums.data,
            prefix_sums.data, uint32(prefix_sums.strides[0]),
            label_count.data,
            wait_for=[event]
        )

        return event, label_count, prefix_sums

    def relabel_with_scanline_order(self, queue, image, labelim, label_root_class_psumim, wait_for = None):
        labelim_result = clarray.empty(queue, tuple(self.img_size), self.label_dtype)
        ldims = self.COMPACT_TILE_COLS, self.COMPACT_TILE_ROWS
        rows, cols = int(self.img_size[0]), int(self.img_size[1])
        r_blocks, c_blocks = divUp(rows, ldims[1]), divUp(cols, ldims[0])
        gdims = (c_blocks * ldims[0], r_blocks * ldims[1])
        event = self._relabel_with_scanline_order(queue,
            gdims, ldims,
            #uint32(rows), uint32(cols),
            labelim_result.data, uint32(labelim_result.strides[0]),
            image.data, uint32(image.strides[0]),
            labelim.data, uint32(labelim.strides[0]),
            label_root_class_psumim.data, uint32(label_root_class_psumim.strides[0]),
            wait_for = wait_for
        )
        return event, labelim_result

    def count_invalid_labels(self, queue, labelim, connectivityim, wait_for = None):
        dcountim = clarray.empty(queue, tuple(self.img_size), uint32)
        ldims = self.COMPACT_TILE_COLS, self.COMPACT_TILE_ROWS
        rows, cols = int(self.img_size[0]), int(self.img_size[1])
        r_blocks, c_blocks = divUp(rows, ldims[1]), divUp(cols, ldims[0])
        gdims = (c_blocks * ldims[0], r_blocks * ldims[1])
        event = self._count_invalid_labels(queue,
            gdims, ldims,
            #uint32(rows), uint32(cols),
            labelim.data, uint32(labelim.strides[0]),
            connectivityim.data, uint32(connectivityim.strides[0]),
            dcountim.data, uint32(dcountim.strides[0]),
            wait_for = wait_for
        )
        return event, dcountim

    def __call__(self, queue, cl_img, wait_for = None, all_outputs = False):
        event, connectivityim = self.make_connectivity_image(queue, cl_img, wait_for=wait_for)
        event, labelim = self.label_tiles(queue, connectivityim, wait_for = [event])

        event, = self.merge_tiles(queue, connectivityim, labelim, wait_for = [event])

        event, = self.compact_paths(queue, labelim, wait_for = [event])
        event, label_count, prefix_sums = self.mark_roots_and_make_prefix_sums(queue, cl_img, labelim, wait_for = [event])
        event, relabel_result = self.relabel_with_scanline_order(queue, cl_img, labelim, prefix_sums, wait_for = [event])
        if all_outputs:
            return event, label_count, relabel_result, labelim, prefix_sums, connectivityim
        else:
            return event, label_count, relabel_result
