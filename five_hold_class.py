import os

from matplotlib import pyplot as plt
from sklearn.metrics import RocCurveDisplay, auc, roc_auc_score

from train import training, evaluate

from utils import get_pos_neg_ratio

os.environ['CUDA_VISIBLE_DEVICES'] = '0'
from os.path import join, exists, basename
import argparse
import numpy as np
import paddle
import tqdm
import paddle.nn as nn
import datetime
from pahelix.model_zoo.gem_model import GeoGNNModel
from pahelix.utils import load_json_config
from pahelix.datasets.inmemory_dataset import InMemoryDataset
import prettytable
# from gtrick import FLAG
from src.model import DownstreamModel
from src.featurizer import DownstreamTransformFn, DownstreamCollateFn
from src.utils import get_dataset, create_splitter, get_downstream_task_names, \
    calc_rocauc_score, exempt_parameters
from sklearn.model_selection import StratifiedKFold

print(paddle.device.get_device())


def build_model(encoder_lr, head_lr, init_model, configs):
    ### build model

    compound_encoder = GeoGNNModel(configs[0])
    model = DownstreamModel(configs[1], compound_encoder)
    criterion = nn.BCELoss(reduction='mean')
    encoder_params = compound_encoder.parameters()
    head_params = exempt_parameters(model.parameters(), encoder_params)

    collate_fn = DownstreamCollateFn(
        atom_names=configs[0]['atom_names'],
        bond_names=configs[0]['bond_names'],
        bond_float_names=configs[0]['bond_float_names'],
        bond_angle_float_names=configs[0]['bond_angle_float_names'],
        task_type='class')

    encoder_opt = paddle.optimizer.AdamW(encoder_lr, parameters=encoder_params, weight_decay=0.01)
    head_opt = paddle.optimizer.AdamW(head_lr, parameters=head_params, weight_decay=0.01)
    print('Total param num: %s' % (sum(p.numel().numpy()[0] for p in model.parameters())))
    print('Encoder param num: %s' % (sum(p.numel().numpy()[0] for p in compound_encoder.parameters())))
    print('Head param num: %s' % (sum(p.numel().numpy()[0] for p in head_params)))
    for i, param in enumerate(model.named_parameters()):
        print(i, param[0], param[1].name)
    # ????????????GEM??????
    #
    if not init_model is None and not init_model == "":
        compound_encoder.set_state_dict(paddle.load(init_model))
        print('Load state_dict from %s' % init_model)
    # ???????????????????????????
    # model.set_state_dict(paddle.load('pretrain_models-chemrl_gem/dilirank_model.pdparams'))

    return model, encoder_opt, head_opt, criterion, collate_fn


def main(args):
    compound_encoder_config = load_json_config(args.compound_encoder_config[1])
    if not args.dropout_rate is None:
        compound_encoder_config['dropout_rate'] = args.dropout_rate

    model_config = load_json_config(args.model_config)
    if not args.dropout_rate is None:
        model_config['dropout_rate'] = args.dropout_rate
    task_names = get_downstream_task_names(args.dataset_name, args.data_path)
    model_config['task_type'] = 'class'
    model_config['num_tasks'] = len(task_names)

    dataset_list = data_process(task_names, args)
    n = 0
    mean_fpr = np.linspace(0, 1, 100)
    tprs = []
    aucs = []
    fig, ax = plt.subplots()

    for train_dataset, test_dataset, valid_dataset in dataset_list:
        master = 0
        master1 = 0
        n += 1
        list_test, list_train, list_auc, list_valid = [], [], [], []
        ps_tool = Plot_save(args, train_dataset, test_dataset, valid_dataset)
        ps_tool.save_split_data(n)

        if n == 1:
            ex_dataset = InMemoryDataset(data_list=train_dataset.data_list + valid_dataset.data_list)

            model, encoder_opt, head_opt, criterion, collate_fn = build_model(args.encoder_lr, args.head_lr,
                                                                              args.init_params,
                                                                              [compound_encoder_config, model_config])
            for epoch_id in tqdm.trange(args.max_epoch):
                ex_loss, ex_auc, ex_table, ex_gra, ex_label = training(args, model, ex_dataset,
                                                                       collate_fn,
                                                                       criterion, encoder_opt, head_opt)
                ext_loss, ext_auc, ext_table, ext_gra, ext_label, ext_pred, mcc = evaluate(args, model, test_dataset,
                                                                                      collate_fn)
                # print(ex_table)

                if ext_auc > master1:
                    master1 = ext_auc / 1.
                    best_label = ext_label / 1.
                    best_pred = ext_pred / 1.
                    ps_tool.save_model(model, epoch_id)
                    print(master1)

                    print(ext_table)
            v = RocCurveDisplay.from_predictions(best_label, best_pred)
            ex_interp_tpr = np.interp(mean_fpr, v.fpr, v.tpr)
            ex_interp_tpr[0] = 0.0
            ex_aucs = v.roc_auc
        model, encoder_opt, head_opt, criterion, collate_fn = build_model(args.encoder_lr, args.head_lr,
                                                                          args.init_params,
                                                                          [compound_encoder_config, model_config])
        for epoch_id in tqdm.trange(args.max_epoch):
            train_loss, train_auc, train_table, train_gra, train_label = training(args, model, train_dataset,
                                                                                  collate_fn,
                                                                                  criterion, encoder_opt, head_opt)
            test_loss, test_auc, test_table, test_gra, test_label, test_pred = evaluate(args, model, test_dataset,
                                                                                        collate_fn)
            valid_loss, valid_auc, valid_table, valid_gra, valid_label, valid_pred = evaluate(args, model,
                                                                                              valid_dataset,
                                                                                              collate_fn)
            if test_auc > master:
                master = test_auc / 1.0
                best_label = test_label / 1.
                best_pred = test_pred / 1.



            list_train.append(train_table._rows[0])
            list_test.append(test_table._rows[0])
            list_valid.append(valid_table._rows[0])
            list_auc.append([train_auc, test_auc, valid_auc])

            train_table.add_row(test_table._rows[0])
            train_table.add_row(valid_table._rows[0])
            fieldname = 'Type'
            train_table._field_names.insert(0, fieldname)
            train_table._align[fieldname] = 'c'
            train_table._valign[fieldname] = 't'
            for i, a in enumerate(['train', 'test', 'valid']):
                train_table._rows[i].insert(0, a)
            print(train_table)

        viz = RocCurveDisplay.from_predictions(best_label, best_pred, name="ROC fold {}".format(n), alpha=0.3, lw=1,
                                               ax=ax)
        interp_tpr = np.interp(mean_fpr, viz.fpr, viz.tpr)
        interp_tpr[0] = 0.0
        tprs.append(interp_tpr)
        aucs.append(viz.roc_auc)

        list_train = np.array(list_train)[:, 1:].astype('float64')
        list_test = np.array(list_test)
        list_valid = np.array(list_valid)

        best = prettytable.PrettyTable()
        best.field_names = ['Result', 'Loss', 'AUC', 'Accuracy', 'Precison', 'Recall', 'F1_score', 'Specificity', 'MCC']
        best.add_row(['best_train', list_train[:, 0].min()] + list(list_train[:, 1:].max(0)))
        best.add_row(['best_test', list_test[:, 0].min()] + list(list_test[:, 1:].max(0)))
        best.add_row(['best_valid', list_valid[:, 0].min()] + list(list_valid[:, 1:].max(0)))
        print(best)

        # ps_tool.plot_auc(n, list_auc)
    ps_tool.plot_fold(ax, tprs, mean_fpr, aucs, [ex_interp_tpr, ex_aucs])


def data_process(task_names, args):
    if args.task == 'data':
        print('Preprocessing data...')
        dataset = get_dataset(args.dataset_name, args.data_path, task_names)
        dataset.transform(DownstreamTransformFn(), num_workers=args.num_workers)
        dataset.save_data(args.cached_data_path)
        return
    else:
        if args.cached_data_path is None or args.cached_data_path == "":
            print('Processing data...')
            dataset = get_dataset(args.dataset_name, args.data_path, task_names)
            dataset.transform(DownstreamTransformFn(), num_workers=args.num_workers)
        else:
            print('Read preprocessing data...')
            dataset = InMemoryDataset(npz_data_path=args.cached_data_path)

    if os.path.isfile(f'data_pro/random/{args.dataset_name}_smiles_train.npy') is True:
        train_s = np.load(f'data_pro/random/{args.dataset_name}_smiles_train.npy')
        label_s = []
        train_dataset_ = []
        test_dataset = []
        dataset_list = []
        data = dataset.data_list
        for i in range(data.__len__()):
            if data[i]['smiles'] in train_s:
                train_dataset_.append(data[i])
                label_s.append(data[i]['label'])
            else:
                test_dataset.append(data[i])

        test_dataset = InMemoryDataset(data_list=test_dataset)

        if args.five_fold == True:
            train_dataset_ = np.array(train_dataset_)
            KF = StratifiedKFold(n_splits=5, shuffle=True, random_state=1024)
            n = 0
            for t in KF.split(train_dataset_, label_s):
                n += 1
                train_dataset, valid_dataset = list(np.array(train_dataset_)[t[0]]), list(
                    np.array(train_dataset_)[t[1]])
                train_dataset, valid_dataset = InMemoryDataset(data_list=train_dataset), InMemoryDataset(
                    data_list=valid_dataset)
                dataset_list.append([train_dataset, test_dataset, valid_dataset])
        else:
            train_dataset = InMemoryDataset(data_list=train_dataset_)
            dataset_list.append([train_dataset, test_dataset])
    else:
        splitter = create_splitter(args.split_type)
        dataset_list = splitter.split(
            dataset, frac_train=0.8, frac_valid=0, frac_test=0.2)
    return dataset_list


class Plot_save:
    def __init__(self, args, train_dataset, test_dataset, valid_dataset):
        self.args = args
        self.dataset = [train_dataset, test_dataset, valid_dataset]

    def save_model(self, model, epoch_id, ):
        print('Saving!')
        paddle.save(model.compound_encoder.state_dict(),
                    '%s/epoch%d/compound_encoder.pdparams' % (self.args.model_dir, epoch_id))
        paddle.save(model.state_dict(),
                    '%s/epoch%d/model.pdparams' % (self.args.model_dir, epoch_id))

    def save_data(self, data):
        np.save(f'data_pro/random/{self.args.dataset_name}_graph_repr.npy', np.concatenate(data[1], 0))
        np.save(f'data_pro/random/{self.args.dataset_name}_graph_label.npy', np.concatenate(data[2], 0))
        with open('test.txt', 'a') as f:
            f.write(
                f'down_lr={self.args.head_lr}, GEM_lr={self.args.encoder_lr}, dropout={self.args.dropout_rate}, batch_size={self.args.batch_size}, split_type={self.args.split_type}' + '\n')
            f.write(datetime.datetime.now().strftime("%d_%H_%M_%S") + '\n')
            f.write(data[0].get_string() + '\n')

    def plot_auc(self, n, list_auc):
        np.save(f'f5/{n}_fold', list_auc)
        # ???'g???'?????????green???,????????????????????????????????????-???????????????????????????????????????????????????label???????????????????????????????????????????????????????????????u?????????????????????????????????????????????????????????????????????????????????
        plt.plot(range(list_auc.__len__()), list_auc, label=['train_auc', 'test_auc', 'valid_auc'])
        plt.legend()
        plt.xlabel(u'epoch')
        plt.ylabel(u'loss')
        plt.show()

    def plot_fold(self, ax, tprs, mean_fpr, aucs, ex):
        ax.plot([0, 1], [0, 1], linestyle="--", lw=2, color="r", label="Chance", alpha=0.8)
        mean_tpr = np.mean(tprs, axis=0)
        mean_tpr[-1] = 1.0
        mean_auc = auc(mean_fpr, mean_tpr)
        std_auc = np.std(aucs)
        ax.plot(mean_fpr, mean_tpr, color="b", label=r"Mean ROC (AUC = %0.2f $\pm$ %0.2f)" % (mean_auc, std_auc), lw=2,
                alpha=0.8)
        ax.plot(mean_fpr, ex[0], color="g", label=r"Test ROC (AUC = %0.2f)" % ex[1], lw=2, alpha=0.8)

        std_tpr = np.std(tprs, axis=0)
        tprs_upper = np.minimum(mean_tpr + std_tpr, 1)
        tprs_lower = np.maximum(mean_tpr - std_tpr, 0)
        ax.fill_between(mean_fpr, tprs_lower, tprs_upper, color="grey", alpha=0.2, label=r"$\pm$ 1 std. dev.", )

        ax.set(xlim=[-0.05, 1.05], ylim=[-0.05, 1.05], title="Receiver operating characteristic curve", )
        ax.legend(loc="lower right")
        plt.show()

    def save_split_data(self, n):
        smiles_train = []
        label_train = []
        for i in range(self.dataset[0].data_list.__len__()):
            smiles_train.append(self.dataset[0].data_list[i]['smiles'])
            label_train.append(self.dataset[0].data_list[i]['label'])

        smiles_test = []
        label_test = []
        for i in range(self.dataset[1].data_list.__len__()):
            smiles_test.append(self.dataset[1].data_list[i]['smiles'])
            label_test.append(self.dataset[1].data_list[i]['label'])

        smiles_valid = []
        label_valid = []
        for i in range(self.dataset[2].data_list.__len__()):
            smiles_valid.append(self.dataset[2].data_list[i]['smiles'])
            label_valid.append(self.dataset[2].data_list[i]['label'])

        print("Train/Valid/Test num: %s/%s/%s" % (
            len(self.dataset[0]), len(self.dataset[2]), len(self.dataset[1])))
        print('Train pos/neg ratio %s/%s' % get_pos_neg_ratio(self.dataset[0]))
        print('Valid pos/neg ratio %s/%s' % get_pos_neg_ratio(self.dataset[2]))
        print('Test pos/neg ratio %s/%s' % get_pos_neg_ratio(self.dataset[1]))
        if self.args.five_fold == True:
            if os.path.isdir(f'f5/{self.args.split_type}_split') is False:
                os.mkdir(f'f5/{self.args.split_type}_split')
            dir_path = f'f5/{self.args.split_type}_split'
        else:
            if os.path.isdir(f'{self.args.split_type}_split') is False:
                os.mkdir(f'{self.args.split_type}_split')
            dir_path = f'{self.args.split_type}_split'
        np.save(
            f'{dir_path}/{self.args.dataset_name}_{n}_smiles_train.npy',
            smiles_train)
        np.save(
            f'{dir_path}/{self.args.dataset_name}_{n}_smiles_test.npy',
            smiles_test)
        np.save(
            f'{dir_path}/{self.args.dataset_name}_{n}_smiles_valid.npy',
            smiles_valid)
        np.save(
            f'{dir_path}/{self.args.dataset_name}_{n}_label_train.npy',
            label_train)
        np.save(
            f'{dir_path}/{self.args.dataset_name}_{n}_label_test.npy',
            label_test)
        np.save(
            f'{dir_path}/{self.args.dataset_name}_{n}_label_valid.npy',
            label_valid)

# if __name__ == '__main__':
#     parser = argparse.ArgumentParser()
#     parser.add_argument("--task", choices=['train', 'data'], default='train')
#
#     parser.add_argument("--batch_size", type=int, default=32)
#     parser.add_argument("--num_workers", type=int, default=2)
#     parser.add_argument("--max_epoch", type=int, default=100)
#     parser.add_argument("--dataset_name",
#                         choices=['bace', 'bbbp', 'clintox', 'hiv',
#                                  'muv', 'sider', 'tox21', 'toxcast'])
#     parser.add_argument("--data_path", type=str, default=None)
#     parser.add_argument("--cached_data_path", type=str, default=None)
#     parser.add_argument("--split_type",
#                         choices=['random', 'scaffold', 'random_scaffold', 'index'])
#
#     parser.add_argument("--compound_encoder_config", type=str)
#     parser.add_argument("--model_config", type=str)
#     parser.add_argument("--init_model", type=str)
#     parser.add_argument("--model_dir", type=str)
#     parser.add_argument("--encoder_lr", type=float, default=0.001)
#     parser.add_argument("--head_lr", type=float, default=0.001)
#     parser.add_argument("--dropout_rate", type=float, default=0.2)
#     parser.add_argument("--exp_id", type=int, help='used for identification only')
#     args = parser.parse_args()
#
#     main(args)
