import matplotlib as mpl
mpl.use('Agg')
import matplotlib.pyplot as plt
from pyspark import SparkContext, SparkConf
from pyspark.mllib.regression import LabeledPoint
from pyspark.mllib.linalg import Vectors
from pyspark.mllib.tree import RandomForest, RandomForestModel
from pyspark.mllib.util import MLUtils
from pyspark.mllib.feature import StandardScaler, StandardScalerModel
from pyspark.mllib.stat import Statistics
from pyspark.mllib.linalg.distributed import RowMatrix
from pyspark.mllib.tree import DecisionTreeModel
from dataset import *
from debugger import *
import math
import random
import datetime
from datetime import timedelta


# setup spark context and config
# conf = SparkConf().setAppName("test")

# conf = SparkConf().setAppName("Print Elements of RDD")\
#     .setMaster("local[4]").set("spark.executor.memory","1g");

sc = SparkContext.getOrCreate()
sc.setLogLevel("ERROR")

myDebugger = Debugger()



class ActiveLearner:
    '''This is the base class for active learning models'''

    def __init__(self, dataset, nEstimators, name):
        '''input: dataset -- an object of class Dataset or any inheriting classes
                  nEstimators -- the number of estimators for the base classifier, usually set to 50
                  name -- name of the method for saving the results later'''

        self.dataset = dataset
        self.indicesKnown = dataset.indicesKnown
        self.indicesUnknown = dataset.indicesUnknown
        # base classification model
        self.nEstimators = nEstimators
        self.model = None
        self.name = name


    def reset(self):
        '''forget all the points sampled by active learning and set labelled
         and unlabelled sets to default of the dataset'''
        self.indicesKnown = self.dataset.indicesKnown
        self.indicesUnknown = self.dataset.indicesUnknown




    def train(self):
        '''train the base classification model on currently available datapoints'''

        # first fetch the subset of training data which match the indices of the known indices
        myDebugger.TIMESTAMP('fetch known data')
        self.trainDataKnown = self.indicesKnown.map(lambda _ : (_, None))\
            .leftOuterJoin(self.dataset.trainSet)\
            .map(lambda _ : (_[0], _[1][1]))

        myDebugger.TIMESTAMP('fetch known data finish')
        # train a RFclassifer with this data
        self.model = RandomForest.trainClassifier(self.trainDataKnown.map(lambda _ : _[1]),
                                                  numClasses=2,
                                                  categoricalFeaturesInfo={},
                                                  numTrees=self.nEstimators,
                                                  featureSubsetStrategy="auto",
                                                  impurity='gini')






        # treeRdd = DecisionTreeModel(self.model._java_model.trees()[0])
        # myDebugger.DEBUG(treeRdd.predict(testData).collect())

        # trees = [DecisionTreeModel(self.model._java_model.trees()[i]) for i in range(100)]
        #
        #
        # predictions = [t.predict(testData) for t in trees]
        #
        # for pred in predictions:
        #     myDebugger.DEBUG(pred.count())


    # def evaluate(self, performanceMeasures):
    #
    #     '''evaluate the performance of current classification for a given set of performance measures
    #     input: performanceMeasures -- a list of performance measure that we would like to estimate. Possible values are 'accuracy', 'TN', 'TP', 'FN', 'FP', 'auc'
    #     output: performance -- a dictionary with performanceMeasures as keys and values consisting of lists with values of performace measure at all iterations of the algorithm'''
    #     performance = {}
    #     test_prediction = self.model.predict(self.dataset.testData)
    #     m = metrics.confusion_matrix(self.dataset.testLabels, test_prediction)
    #
    #     if 'accuracy' in performanceMeasures:
    #         performance['accuracy'] = metrics.accuracy_score(self.dataset.testLabels, test_prediction)
    #
    #     if 'TN' in performanceMeasures:
    #         performance['TN'] = m[0, 0]
    #     if 'FN' in performanceMeasures:
    #         performance['FN'] = m[1, 0]
    #     if 'TP' in performanceMeasures:
    #         performance['TP'] = m[1, 1]
    #     if 'FP' in performanceMeasures:
    #         performance['FP'] = m[0, 1]
    #
    #     if 'auc' in performanceMeasures:
    #         test_prediction = self.model.predict_proba(self.dataset.testData)
    #         test_prediction = test_prediction[:, 1]
    #         performance['auc'] = metrics.roc_auc_score(self.dataset.testLabels, test_prediction)
    #
    #     return performance





class DistributedActiveLearnerRandom(ActiveLearner):
    '''Randomly samples the points'''

    def selectNext(self):
        # permuting unlabeled instances randomly
        self.indicesUnknown = self.indicesUnknown\
            .sortBy(lambda _: random.random())

        # takes the first from the unknown samples and add it to the known ones
        self.indicesKnown = self.indicesKnown\
            .union(sc.parallelize(self.indicesUnknown.take(1)))

        # removing first sample from unlabeled ones(update)
        first = self.indicesUnknown.first()
        self.indicesUnknown = self.indicesUnknown.filter(lambda _ : _ != first)
        myDebugger.DEBUG(self.indicesKnown.collect())








class DistributedActiveLearnerUncertainty(ActiveLearner):
    '''Points are sampled according to uncertainty sampling criterion'''

    def selectNext(self):
        # predict for the rest the datapoints
        self.trainDataUnknown = self.indicesUnknown.map(lambda _: (_, None)) \
            .leftOuterJoin(self.dataset.trainSet) \
            .map(lambda _: (_[0], _[1][1]))

        actualIndices = self.trainDataUnknown.map(lambda _ : _[0])\
            .zipWithIndex()\
            .map(lambda _: (_[1], _[0]))

        myDebugger.TIMESTAMP('zipping indices ')


        rdd = sc.parallelize([])

        ''' these java objects are not serializable
         thus still no support to make an RDD out of it!! <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<
        '''
        for x in self.model._java_model.trees():
            '''
             zipping each prediction from each decision tree
             with individual sample index so that they can be
             added later
            '''
            predX = DecisionTreeModel(x)\
                .predict(self.trainDataUnknown.map(lambda _ : _[1].features))\
                .zipWithIndex()\
                .map(lambda _: (_[1], _[0]))

            predX = actualIndices.leftOuterJoin(predX).map(lambda _ : _[1])
            rdd = rdd.union(predX)

        myDebugger.TIMESTAMP('get individual tree predictions')

        ''' adding up no. of 1 in each sample's prediction this is the class prediction of 1s'''
        classPrediction = rdd.groupByKey().mapValues(sum)

        myDebugger.TIMESTAMP('reducing ')


        #  direct self.nEstimators gives error
        totalEstimators = self.nEstimators
        #  predicted probability of class 0
        classPrediction = classPrediction.map(lambda _  : (_[0], abs(0.5 - (1-(_[1]/totalEstimators)))))

        myDebugger.TIMESTAMP('mapping')


        # Selecting the index which has the highest uncertainty/ closest to probability 0.5
        selectedIndex1toN = classPrediction.sortBy(lambda _ : _[1]).first()[0]


        myDebugger.TIMESTAMP('sorting')

        # takes the selectedIndex from the unknown samples and add it to the known ones
        self.indicesKnown = self.indicesKnown .union(sc.parallelize([selectedIndex1toN]))


        myDebugger.TIMESTAMP('update known indices')

        # removing first sample from unlabeled ones(update)
        self.indicesUnknown = self.indicesUnknown.filter(lambda _: _ != selectedIndex1toN)

        myDebugger.TIMESTAMP('update unknown indices')


        myDebugger.DEBUG(selectedIndex1toN)
        myDebugger.DEBUG(self.indicesKnown.collect())
        myDebugger.DEBUG(self.indicesUnknown.collect())


        myDebugger.TIMESTAMP('DEBUGGING DONE')






def getSD( x, totalEstimators):
    sumValue = x[1]
    mean = sumValue / totalEstimators
    sd = math.sqrt((sumValue * ((1 - mean) ** 2) + (totalEstimators - sumValue) * (mean ** 2)) / (totalEstimators-1))
    return (x[0], sd)



class ActiveLearnerLAL(ActiveLearner):
    '''Points are sampled according to a method described in K. Konyushkova, R. Sznitman, P. Fua 'Learning Active Learning from data'  '''

    def __init__(self, dataset, nEstimators, name, lalModel):
        ActiveLearner.__init__(self, dataset, nEstimators, name)
        self.lalModel = lalModel

    def selectNext(self):
        # get predictions from individual trees
        self.trainDataUnknown = self.indicesUnknown.map(lambda _: (_, None)) \
            .leftOuterJoin(self.dataset.trainSet) \
            .map(lambda _: (_[0], _[1][1]))

        # zipping actual indices with dummy indices so that they can be traced later
        actualIndices = self.trainDataUnknown.map(lambda _: _[0]) \
            .zipWithIndex() \
            .map(lambda _: (_[1], _[0]))

        # an empty RDD
        rdd = sc.parallelize([])

        ''' these java objects are not serializable
         thus still no support to make an RDD out of it!! <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<
        '''
        for x in self.model._java_model.trees():
            # zipping each prediction from each decision tree with individual sample index so that they can be added later
            predX = DecisionTreeModel(x) \
                .predict(self.trainDataUnknown.map(lambda _: _[1].features)) \
                .zipWithIndex() \
                .map(lambda _: (_[1], _[0]))
            predX = actualIndices.leftOuterJoin(predX).map(lambda _: _[1])
            rdd = rdd.union(predX)


        ''' adding up no. of 1 in each sample's prediction this is the class prediction of 1s'''
        sumScore = rdd.groupByKey().mapValues(sum)
        totalEstimators = self.nEstimators


        # average of the predicted scores
        f_1 = sumScore.map(lambda _: (_[0], _[1] / totalEstimators))

        # standard deviation of predicted scores
        f_2 = sumScore.map(lambda _ : getSD(_,totalEstimators))

        # - proportion of positive points
        nLabeled = self.trainDataKnown.count()
        nUnlabeled = self.trainDataUnknown.count()
        proportionPositivePoints = (self.trainDataKnown.map(lambda _ : _[1].label).reduce(lambda x,y : x+y)) / nLabeled
        f_3 = f_1.map(lambda _ : proportionPositivePoints)

        # - estimate variance of forest by looking at avergae of variance of some predictions
        estimateVariance = (f_2.map(lambda _ : _[1]).reduce(lambda x,y : x+y)) / nUnlabeled
        f_6 = f_3.map(lambda _ : estimateVariance)

        # - number of already labelled datapoints
        f_8 = f_3.map(lambda _ : nLabeled)



        myDebugger.TIMESTAMP('features ready for transposing')

        # transposing start
        tempf_1 = f_1.map(lambda _ : _[1]).zipWithIndex().map(lambda _ : (_[1],_[0]))
        tempf_2 = f_2.map(lambda _: _[1]).zipWithIndex().map(lambda _ : (_[1],_[0]))
        tempf_3 = f_3.zipWithIndex().map(lambda _ : (_[1],_[0]))
        tempf_6 = f_6.zipWithIndex().map(lambda _ : (_[1],_[0]))
        tempf_8 = f_8.zipWithIndex().map(lambda _ : (_[1],_[0]))
        LALDataset = tempf_1\
            .leftOuterJoin(tempf_2)\
            .leftOuterJoin(tempf_3)\
            .leftOuterJoin(tempf_6)\
            .leftOuterJoin(tempf_8)\
            .map(lambda _  : LabeledPoint(_[0] ,
                              [_[1][0][0][0][0],  _[1][0][0][0][1],  _[1][0][0][1], _[1][0][1], _[1][1]]))

        myDebugger.TIMESTAMP('transposing done')

        # # predict the expected reduction in the error by adding the point
        LALprediction = self.lalModel.predict(LALDataset.map(lambda _ : _.features))\
            .zipWithIndex()\
            .map(lambda _ : (_[1],_[0]))
        myDebugger.TIMESTAMP('prediction done')




        # Selecting the index which has the highest uncertainty/ closest to probability 0.5
        selectedIndex1toN = LALprediction.sortBy(lambda _: _[1]).max()[0]

        # takes the selectedIndex from the unknown samples and add it to the known ones
        self.indicesKnown = self.indicesKnown.union(sc.parallelize([selectedIndex1toN]))

        # updating unknown indices
        self.indicesUnknown = self.indicesUnknown.filter(lambda _: _ != selectedIndex1toN)



        ''' debugging block '''
        myDebugger.TIMESTAMP('update unknown indices')
        myDebugger.DEBUG(selectedIndex1toN)
        myDebugger.DEBUG(self.indicesKnown.collect())
        myDebugger.DEBUG(self.indicesUnknown.collect())
        myDebugger.TIMESTAMP('DEBUGGING DONE')










# regressionData = sc.textFile('hdfs://node1:9000/input/lal_randomtree_simulatedunbalanced_big.txt')
# reg = regressionData.map(lambda _ : _.split(' '))
# reg = reg.map(lambda _ : LabeledPoint(_[-1] , [_[0],_[1],_[2],_[5],_[7]]))
#
# myDebugger.TIMESTAMP('---------------------------model saving start--------------------------------')
# MODEL_LOCATION = 'hdfs://node1:9000/regression_model'
# try:
#     lalmodel = RandomForestModel.load(sc, MODEL_LOCATION)
# except:
#     lalmodel = RandomForest.trainRegressor(reg, numTrees=2000, categoricalFeaturesInfo={})
#     lalmodel.save(sc, MODEL_LOCATION)
# myDebugger.TIMESTAMP('---------------------------model saving finish--------------------------------')



dtst = DatasetCheckerboard2x2()
dtst.setStartState(2)

X = []
y = []

alR = DistributedActiveLearnerRandom(dtst, 50 , 'random')
for i in range(990):
    alR.train()
    alR.selectNext()
    x = myDebugger.TIMESTAMP('MODEL TRAINED!!')
    y.append(x)
    X.append(i+1)

plt.plot(X,y)
plt.savefig('alrandom_first.png')
# alLALindepend = ActiveLearnerLAL(dtst, 50, 'lal-rand', lalmodel )
# alLALindepend.train()
# myDebugger.TIMESTAMP('MODEL TRAINED!!')
# alLALindepend.selectNext()


