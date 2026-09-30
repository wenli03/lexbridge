package com.lexbridge.application.dto;

import java.util.List;

/**
 * 分页结果。
 *
 * <p>与前端 {@code Page<T>} 一一对应（见《详细设计》§2.2）。
 *
 * <p><b>为什么显式回传 {@code page} 与 {@code size}，而不是只给 items 与 total。</b>
 * 应用层会钳制入参（页码至少为 1、每页条数上限 100），也就是**返回的 size 未必等于请求的 size**。
 * 若只回传 items 与 total，前端按自己请求的 size 算出的总页数会与实际不符，
 * 表现为"翻到某页永远是空的"。
 *
 * @param items 当前页数据
 * @param total 满足条件的总条数
 * @param page  实际生效的页码（从 1 开始）
 * @param size  实际生效的每页条数
 */
public record PageResult<T>(List<T> items, long total, int page, int size) {
}
